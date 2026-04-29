from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.adapters.base import Tick
from app.config import get_settings

log = logging.getLogger("taiex.adapter.finmind")

FINMIND_URL = "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot"
SOURCE = "FINMIND_FUTURES_SNAPSHOT"


class FinMindTaiexAdapter:
    """Polls FinMind sponsor `taiwan_futures_snapshot` endpoint.

    Snapshot is real-time only: each call returns the latest tick for `data_id`
    (TXF / TMF / CDF). Display symbol is decoupled from source so the rest of
    the stack still labels rows with `symbol_display` (e.g. MXF).
    """

    def __init__(self, display_symbol: str | None = None) -> None:
        s = get_settings()
        self.symbol = display_symbol or s.symbol_display
        self.source = SOURCE
        self._token = s.finmind_token
        self._tz = s.tz
        self._poll = s.poll_interval_sec
        self._open = s.market_open
        self._close = s.market_close
        self._data_id = s.symbol_source
        self._last_seen_ts: datetime | None = None

    async def stream_ticks(self) -> AsyncIterator[Tick]:
        while True:
            now = datetime.now(self._tz)
            if not self._market_open(now):
                until = self._next_open(now)
                wait_s = max((until - now).total_seconds(), 30)
                log.info("market closed; sleeping %.0fs until next open", wait_s)
                await asyncio.sleep(min(wait_s, 60))
                continue

            try:
                rows = await self._fetch()
            except Exception:
                log.exception("FinMind fetch failed; backing off")
                await asyncio.sleep(self._poll * 2)
                continue

            new_ticks = self._dedupe(rows)
            for t in new_ticks:
                yield t
            await asyncio.sleep(self._poll)

    async def backfill(self, start: datetime, end: datetime) -> list[Tick]:
        # Snapshot endpoint has no history. Return empty so runner skips backfill.
        log.info("backfill skipped: taiwan_futures_snapshot is real-time only")
        return []

    def _market_open(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        return self._open <= now.timetz().replace(tzinfo=None) <= self._close

    def _next_open(self, now: datetime) -> datetime:
        candidate = now.replace(
            hour=self._open.hour, minute=self._open.minute, second=0, microsecond=0
        )
        if now >= candidate:
            candidate = candidate + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate = candidate + timedelta(days=1)
        return candidate

    async def _fetch(self) -> list[dict]:
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        params: dict[str, str] = {}
        if self._data_id:
            params["data_id"] = self._data_id

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=4),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=20) as cli:
                    resp = await cli.get(FINMIND_URL, headers=headers, params=params)
                    resp.raise_for_status()
                    payload = resp.json()
        if "data" not in payload:
            log.warning("unexpected FinMind payload: %s", payload)
            return []
        return payload["data"]

    def _rows_to_ticks(self, rows: list[dict]) -> list[Tick]:
        out: list[Tick] = []
        for r in rows:
            raw_ts = r.get("date")
            raw_price = r.get("close")
            if raw_price is None:
                raw_price = r.get("price")
            if raw_ts is None or raw_price is None:
                continue
            try:
                ts = datetime.fromisoformat(str(raw_ts)).replace(tzinfo=self._tz)
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            out.append(Tick(ts=ts, symbol=self.symbol, price=price, source=self.source))
        return out

    def _dedupe(self, rows: list[dict]) -> list[Tick]:
        ticks = self._rows_to_ticks(rows)
        if not ticks:
            return []
        if self._last_seen_ts is None:
            new = ticks
        else:
            new = [t for t in ticks if t.ts > self._last_seen_ts]
        if new:
            self._last_seen_ts = max(t.ts for t in new)
        return new
