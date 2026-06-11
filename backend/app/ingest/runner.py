from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.adapters.base import MarketDataAdapter, Tick
from app.adapters.shioaji_taiex import ShioajiFuturesAdapter
from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Tick as TickRow
from app.strategies.base import in_market_session

log = logging.getLogger("taiex.ingest")

_BAR_BUFFER_MAXLEN = 600

RESOLUTIONS = ["1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "12h", "1d", "1w", "1mo"]
RESOLUTION_DELTAS = {
    "1m": timedelta(minutes=1),
    "2m": timedelta(minutes=2),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "10m": timedelta(minutes=10),
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

    def __init__(
        self, adapter: MarketDataAdapter | None = None, hub: Any | None = None
    ) -> None:
        self._settings = get_settings()
        self._adapter: MarketDataAdapter = adapter or ShioajiFuturesAdapter()
        # Optional NotifierHub for operational (non-trading) alerts fired by the
        # feed-health watchdog. None → alerts are logged only.
        self._hub = hub
        self._task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._feed_health_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._open_buckets: dict[str, datetime] = {}
        # Tombstones: bucket starts that have already been bar_close'd, kept
        # per-resolution so a delayed tick for an already-retired bucket can
        # be ignored instead of re-seeding `_open_buckets` and producing a
        # second close on the next bucket boundary.
        self._closed_buckets: dict[str, list[datetime]] = defaultdict(list)
        self._last_tick: Tick | None = None
        # Per-resolution OHLC accumulator for the currently-open bucket. On
        # bucket boundary the accumulator is finalized into ``_closed_bars``.
        # Strategies read from ``_closed_bars`` via ``snapshot_bars`` — this
        # is the strategy-path source of truth and bypasses the cagg refresh
        # lag entirely (cagg remains source of truth for the /bars REST + UI
        # + backtest paths).
        self._bucket_ohlc: dict[str, dict[str, Any]] = {}
        self._closed_bars: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=_BAR_BUFFER_MAXLEN)
        )
        # Per-resolution last bar_close timestamp. Powers /status liveness.
        self._last_close_ts: dict[str, datetime] = {}
        # Per-resolution count of subscriber-queue overflow drops.
        self._dropped: dict[str, int] = defaultdict(int)
        # --- Feed-health watchdog state ---------------------------------
        # Baseline for "time since last tick" before the first tick arrives;
        # set in start() so a feed that never delivers a tick during a session
        # still trips the watchdog.
        self._feed_started_at: datetime | None = None
        self._last_reconnect_at: datetime | None = None
        self._reconnect_count = 0          # lifetime forced reconnects
        self._reconnect_failures = 0       # consecutive failures → backoff
        self._session_reconnects = 0       # reconnects in current session window

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._ready.clear()
        # Pre-ready phase: hydrate the in-memory bar buffer from cagg AND
        # backfill recent ticks BEFORE flipping the ready event. Subscribers
        # that await `ready()` (e.g. StrategyLoop) are guaranteed a warm
        # buffer + recent ticks persisted before the live tick stream begins.
        # If hydration ran AFTER `_ready.set()`, `snapshot_bars` could return
        # stale data while live ticks were already flowing.
        await self._hydrate_bar_buffer()
        try:
            await self._backfill_recent()
        except Exception:
            log.exception("backfill failed; continuing to live ingest")
        self._ready.set()
        self._feed_started_at = datetime.now(self._settings.tz)
        self._task = asyncio.create_task(self._run(), name="ingest-runner")
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="ingest-watchdog"
        )
        # Feed-health runs in its OWN task: a forced reconnect can await
        # logout+login for tens of seconds (login cooldown), and we must not
        # block the bucket-force-close watchdog while that happens.
        self._feed_health_task = asyncio.create_task(
            self._feed_health_loop(), name="ingest-feed-health"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._feed_health_task is not None:
            self._feed_health_task.cancel()
            try:
                await self._feed_health_task
            except (asyncio.CancelledError, Exception):
                pass
            self._feed_health_task = None

    def subscribe(self, resolution: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self._subscribers[resolution].add(q)
        return q

    def unsubscribe(self, resolution: str, q: asyncio.Queue) -> None:
        self._subscribers[resolution].discard(q)

    @property
    def last_tick(self) -> Tick | None:
        return self._last_tick

    async def ready(self) -> None:
        """Block until the bar buffer has been hydrated from cagg."""
        await self._ready.wait()

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def dropped_counts(self) -> dict[str, int]:
        return dict(self._dropped)

    @property
    def last_close_ts(self) -> dict[str, datetime]:
        return dict(self._last_close_ts)

    def snapshot_bars(self, resolution: str, limit: int | None = None) -> pd.DataFrame:
        """Return the closed bars for ``resolution`` as a pandas DataFrame.

        Index is ``bucket`` (UTC datetime), columns: open / high / low /
        close / tick_count. Returns an empty DataFrame with the expected
        columns when the buffer is cold or the resolution is unknown.
        """
        bars = list(self._closed_bars.get(resolution, ()))
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "tick_count"])
        if limit is not None and limit > 0 and limit < len(bars):
            bars = bars[-limit:]
        df = pd.DataFrame(bars).set_index("bucket")
        # Buffer entries may carry naive (test) or tz-aware (cagg) datetimes.
        # `utc=True` normalises both to a UTC tz-aware index — required for
        # downstream indicator math + matches cagg-via-`load_bars` semantics.
        df.index = pd.to_datetime(df.index, utc=True)
        return df

    async def _hydrate_bar_buffer(self) -> None:
        """Fill the per-resolution closed-bar buffer from cagg.

        Best effort — any failure (DB cold, cagg empty, transient error)
        leaves the affected resolution's deque empty; the live tick path
        will populate it as ticks flow. Strategies tolerate undersized
        windows by returning False from gate evaluation when there are
        not enough bars (e.g. ``MA120`` requires 120 bars, otherwise
        ``_scalar(series, idx=-2)`` returns None and entries block).
        """
        # Local import: app.api.routes.bars imports from app.ingest.runner
        # in some indirect chains; keeping this lazy avoids any import cycle.
        from app.api.routes.bars import load_bars

        symbol = self._settings.symbol_display
        for res in RESOLUTIONS:
            try:
                df = await load_bars(symbol, res, limit=_BAR_BUFFER_MAXLEN)
            except Exception:
                log.exception("hydrate buffer failed for %s; continuing cold", res)
                continue
            if df is None or df.empty:
                continue
            buf = self._closed_bars[res]
            buf.clear()
            for ts, row in df.iterrows():
                buf.append(
                    {
                        "bucket": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "tick_count": int(row.get("tick_count", 0) or 0),
                    }
                )
            if buf:
                self._last_close_ts[res] = buf[-1]["bucket"]

    async def _run(self) -> None:
        # `_backfill_recent` ran during `start()` (pre-ready phase) so the
        # buffer + tick table are warm before live streaming begins.
        while not self._stop.is_set():
            try:
                async for tick in self._adapter.stream_ticks():
                    await self._handle_tick(tick)
                    if self._stop.is_set():
                        break
                # Yield control even when the adapter's generator returns
                # immediately (e.g. test fakes with no ticks), so cancellation
                # via `stop()` can be serviced instead of busy-looping.
                await asyncio.sleep(0)
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
            # If the watchdog already retired this bucket, ignore the tick —
            # otherwise we'd re-seed `_open_buckets[res]` and emit a duplicate
            # bar_close when the next boundary crosses.
            if bucket in self._closed_buckets[res]:
                continue
            prev = self._open_buckets.get(res)
            if prev is not None and bucket != prev:
                await self._emit_close(res, prev)
                self._mark_closed(res, prev)
            self._open_buckets[res] = bucket
            self._update_bucket_ohlc(res, bucket, tick)
            await self._emit_update(res, bucket, tick)

    def _update_bucket_ohlc(self, resolution: str, bucket: datetime, tick: Tick) -> None:
        """Maintain the OHLC accumulator for the currently-open bucket.

        First tick of a bucket initialises (open=high=low=close=price); each
        subsequent tick updates high/low/close and tick_count. The finalised
        OHLC is appended to ``_closed_bars`` inside ``_emit_close``.
        """
        price = float(tick.price)
        st = self._bucket_ohlc.get(resolution)
        if st is None or st["bucket"] != bucket:
            self._bucket_ohlc[resolution] = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "tick_count": 1,
            }
            return
        if price > st["high"]:
            st["high"] = price
        if price < st["low"]:
            st["low"] = price
        st["close"] = price
        st["tick_count"] += 1

    def _finalize_bucket(self, resolution: str, bucket: datetime) -> None:
        """Append the bucket's accumulated OHLC to ``_closed_bars``.

        Append-only: never mutates earlier rows. ``IndicatorCache`` keys on
        ``bars.index[-1]`` so monotonic appends preserve cache correctness.
        Idempotent — calling twice for the same bucket pops the accumulator
        on the first call and is a no-op on the second.
        """
        st = self._bucket_ohlc.get(resolution)
        if st is None or st["bucket"] != bucket:
            return
        self._closed_bars[resolution].append(dict(st))
        self._bucket_ohlc.pop(resolution, None)

    async def _watchdog_tick(self) -> None:
        """Force-close any open bucket that is clearly stale.

        During quiet trading periods (night session, day-night gap, end of
        session) the regular close path waits for the *next* tick to cross a
        boundary. If no tick arrives, the bucket would stay "open" forever.

        Threshold: ``3 * delta`` covers Shioaji's disconnect/reconnect
        latency (the live stream pauses up to ~10s on transient errors plus
        the adapter's re-subscribe round-trip); two buckets of grace is
        enough breathing room before we declare a bucket stale, while still
        catching the long-quiet case before subscribers notice the freeze.
        """
        now = datetime.now(self._settings.tz)
        for res, bucket in list(self._open_buckets.items()):
            delta = RESOLUTION_DELTAS[res]
            if now - bucket >= 3 * delta:
                await self._emit_close(res, bucket)
                # Remove so we don't re-emit on the next pass. Tombstone the
                # bucket so a delayed tick for the same bucket cannot re-seed
                # _open_buckets and trigger a second bar_close.
                self._open_buckets.pop(res, None)
                self._mark_closed(res, bucket)

    def _mark_closed(self, res: str, bucket: datetime) -> None:
        """Record ``bucket`` as already-closed for ``res``; bound history to 4 entries."""
        tombstones = self._closed_buckets[res]
        if bucket in tombstones:
            return
        tombstones.append(bucket)
        if len(tombstones) > 4:
            tombstones.pop(0)

    async def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(5)
                await self._watchdog_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watchdog tick failed; continuing")

    async def _feed_health_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(5)
                await self._check_feed_health()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("feed-health tick failed; continuing")

    # ------------------------------------------------------------------
    # Feed-health watchdog
    # ------------------------------------------------------------------

    def _reset_feed_health(self) -> None:
        """Clear backoff + per-session reconnect counter (feed is healthy or
        the session is closed)."""
        self._reconnect_failures = 0
        self._session_reconnects = 0

    def _reconnect_backoff_delta(self) -> timedelta:
        """Minimum gap before the next forced reconnect — exponential in the
        consecutive-failure count, capped by ``feed_reconnect_backoff_max_sec``."""
        base = self._settings.shioaji_login_cooldown_sec  # 30s default
        secs = base * (2 ** self._reconnect_failures)
        return timedelta(seconds=min(secs, self._settings.feed_reconnect_backoff_max_sec))

    def _in_market_session(self, now: datetime) -> bool:
        s = self._settings
        return in_market_session(
            now,
            s.tz,
            day_open=s.market_open,
            day_close=s.market_close,
            night_open=s.night_session_open,
            night_close=s.night_session_close,
        )

    def feed_silence_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since the last tick (or since start if none yet)."""
        now = now or datetime.now(self._settings.tz)
        ref = self._last_tick.ts if self._last_tick is not None else self._feed_started_at
        if ref is None:
            return None
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=self._settings.tz)
        return (now - ref).total_seconds()

    async def _check_feed_health(self) -> None:
        """Force a full adapter reconnect when ticks stop during an open session.

        The Shioaji SDK can get stuck with its session "down" — auto-reconnect
        on the same instance fails permanently and no exception surfaces to our
        code, so the stream silently starves. This actively detects that and
        drives ``adapter.reconnect()`` (fresh login). Guards:
          - disabled via config kill-switch;
          - only acts inside an open market session (off-hours silence is
            normal — and we reset backoff/session counters there);
          - resets backoff ONLY when ticks actually resume (NOT merely because a
            reconnect call returned), so a "login OK but quote still silent"
            loop cannot burn the 1000/day login budget;
          - hard per-session reconnect cap;
          - exponential backoff between attempts.
        """
        if not self._settings.feed_watchdog_enabled:
            return
        now = datetime.now(self._settings.tz)
        if not self._in_market_session(now):
            self._reset_feed_health()
            return
        silence = self.feed_silence_seconds(now)
        if silence is None or silence < self._settings.feed_stale_seconds:
            self._reset_feed_health()  # ticks flowing — the only healthy reset
            return
        if not hasattr(self._adapter, "reconnect"):
            return  # adapter has no recoverable session (e.g. test/silent)
        if self._session_reconnects >= self._settings.feed_max_reconnects_per_session:
            return  # hard cap reached — stop hammering logins
        if (
            self._last_reconnect_at is not None
            and now - self._last_reconnect_at < self._reconnect_backoff_delta()
        ):
            return  # still inside the backoff window
        self._session_reconnects += 1
        self._reconnect_count += 1
        self._last_reconnect_at = now
        log.warning(
            "feed stale %.0fs; forcing reconnect (#%d this session, %d lifetime)",
            silence,
            self._session_reconnects,
            self._reconnect_count,
        )
        await self._notify_ops(
            f"資料流靜默 {silence:.0f}s — 強制重連 Shioaji "
            f"(本時段第 {self._session_reconnects} 次)"
        )
        try:
            await self._adapter.reconnect()
        except Exception:
            log.exception("forced reconnect raised")
        # NOTE: bump failures regardless of whether reconnect() returned cleanly.
        # Only the "ticks resumed" branch above clears it — a successful login
        # that still yields no ticks must keep backing off.
        self._reconnect_failures += 1

    async def _notify_ops(self, message: str) -> None:
        if self._hub is None:
            return
        try:
            await self._hub.notify_ops(message)
        except Exception:
            log.exception("ops notification failed; continuing")

    @property
    def last_reconnect_ts(self) -> datetime | None:
        return self._last_reconnect_at

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def reconnect_failures(self) -> int:
        return self._reconnect_failures

    def feed_health(self) -> dict[str, Any]:
        """Feed-liveness summary for /status. ``feed_healthy`` is True whenever
        the market is closed (silence is expected) OR ticks are flowing within
        the stale threshold — so off-hours quiet never reads as a fault."""
        now = datetime.now(self._settings.tz)
        in_session = self._in_market_session(now)
        silence = self.feed_silence_seconds(now)
        healthy = (
            (not in_session)
            or silence is None
            or silence < self._settings.feed_stale_seconds
        )
        return {
            "feed_healthy": healthy,
            "in_market_session": in_session,
            "silence_seconds": silence,
            "watchdog_enabled": self._settings.feed_watchdog_enabled,
            "reconnect_count": self._reconnect_count,
            "reconnect_failures": self._reconnect_failures,
            "session_reconnects": self._session_reconnects,
            "last_reconnect_ts": (
                self._last_reconnect_at.isoformat()
                if self._last_reconnect_at is not None
                else None
            ),
        }

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
        # Finalize the in-memory bar buffer FIRST so any subscriber that
        # immediately calls `snapshot_bars` sees the just-closed bucket.
        # Idempotent if called twice for the same bucket.
        self._finalize_bucket(resolution, bucket)
        self._last_close_ts[resolution] = bucket
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
                self._dropped[resolution] += 1
                log.error(
                    "subscriber queue full for %s; dropping %s (total dropped=%d)",
                    resolution,
                    msg.get("type"),
                    self._dropped[resolution],
                )

    async def stream(self, resolution: str) -> AsyncIterator[dict[str, Any]]:
        q = self.subscribe(resolution)
        try:
            while not self._stop.is_set():
                yield await q.get()
        finally:
            self.unsubscribe(resolution, q)
