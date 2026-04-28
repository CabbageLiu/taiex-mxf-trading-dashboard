from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.adapters.base import MarketDataAdapter, Tick
from app.adapters.finmind_taiex import FinMindTaiexAdapter
from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Tick as TickRow

log = logging.getLogger("taiex.ingest")

RESOLUTIONS = ["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w", "1mo"]
RESOLUTION_DELTAS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
    "1mo": timedelta(days=30),
}


def _bucket_start(ts: datetime, resolution: str) -> datetime:
    if resolution == "1mo":
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if resolution == "1w":
        d = ts - timedelta(days=ts.weekday())
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = RESOLUTION_DELTAS[resolution]
    epoch = datetime(1970, 1, 1, tzinfo=ts.tzinfo)
    n = int((ts - epoch) / delta)
    return epoch + n * delta


class IngestRunner:
    """Polls the adapter, persists ticks, fans out bar-close events."""

    def __init__(self, adapter: MarketDataAdapter | None = None) -> None:
        self._settings = get_settings()
        self._adapter: MarketDataAdapter = adapter or FinMindTaiexAdapter()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._open_buckets: dict[str, datetime] = {}
        self._last_tick: Tick | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ingest-runner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def subscribe(self, resolution: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self._subscribers[resolution].add(q)
        return q

    def unsubscribe(self, resolution: str, q: asyncio.Queue) -> None:
        self._subscribers[resolution].discard(q)

    @property
    def last_tick(self) -> Tick | None:
        return self._last_tick

    async def _run(self) -> None:
        try:
            await self._backfill_recent()
        except Exception:
            log.exception("backfill failed; continuing to live ingest")

        while not self._stop.is_set():
            try:
                async for tick in self._adapter.stream_ticks():
                    await self._handle_tick(tick)
                    if self._stop.is_set():
                        break
            except Exception:
                log.exception("adapter stream error; reconnecting in 5s")
                await asyncio.sleep(5)

    async def _backfill_recent(self) -> None:
        end = datetime.now(self._settings.tz)
        start = end - timedelta(days=2)
        ticks = await self._adapter.backfill(start, end)
        if not ticks:
            return
        await self._persist(ticks)
        log.info("backfilled %d ticks", len(ticks))

    async def _handle_tick(self, tick: Tick) -> None:
        self._last_tick = tick
        await self._persist([tick])
        for res in RESOLUTIONS:
            bucket = _bucket_start(tick.ts, res)
            prev = self._open_buckets.get(res)
            if prev is not None and bucket != prev:
                await self._emit_close(res, prev)
            self._open_buckets[res] = bucket
            await self._emit_update(res, bucket, tick)

    async def _persist(self, ticks: list[Tick]) -> None:
        if not ticks:
            return
        rows = [
            {"ts": t.ts, "symbol": t.symbol, "price": t.price, "source": t.source}
            for t in ticks
        ]
        async with session_scope() as s:
            stmt = pg_insert(TickRow).values(rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["ts", "symbol"])
            await s.execute(stmt)
            await s.commit()

    async def _emit_update(self, resolution: str, bucket: datetime, tick: Tick) -> None:
        msg = {
            "type": "bar_update",
            "resolution": resolution,
            "bucket": bucket.isoformat(),
            "price": tick.price,
            "ts": tick.ts.isoformat(),
            "symbol": tick.symbol,
        }
        await self._fanout(resolution, msg)

    async def _emit_close(self, resolution: str, bucket: datetime) -> None:
        msg = {
            "type": "bar_close",
            "resolution": resolution,
            "bucket": bucket.isoformat(),
            "symbol": self._settings.symbol_display,
        }
        await self._fanout(resolution, msg)

    async def _fanout(self, resolution: str, msg: dict[str, Any]) -> None:
        for q in list(self._subscribers.get(resolution, ())):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("subscriber queue full; dropping message")

    async def stream(self, resolution: str) -> AsyncIterator[dict[str, Any]]:
        q = self.subscribe(resolution)
        try:
            while not self._stop.is_set():
                yield await q.get()
        finally:
            self.unsubscribe(resolution, q)
