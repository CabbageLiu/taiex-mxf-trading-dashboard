"""15m trend indicator service.

Computes EMA20 / EMA50 / +DI14 / -DI14 / ADX14 on every 15m bar_close,
classifies trend direction and confidence, persists to ``trends`` table,
broadcasts via NotifierHub.inapp subscribers as ``trend_update`` event.

Cold start: needs >= 50 closed 15m bars before EMA50 stabilises. First few
snapshots may stamp Bullish/Bearish off small samples — acceptable for an
informational signal.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger("taiex.services.trend")


@dataclass(frozen=True)
class TrendSnapshot:
    ts: datetime
    symbol: str
    resolution: str
    ema20: float
    ema50: float
    plus_di: float
    minus_di: float
    adx: float
    direction: int   # -1, 0, +1
    score: float     # direction * min(adx/50, 1.0)
    label: str


def label_for(score: float) -> str:
    """5-band Chinese label binning.

    Boundaries are inclusive on the lower end of the "stronger" band so a
    score of exactly +0.70 reads as 強勢上升 (not 溫和上升).
    """
    if score >= 0.70:
        return "強勢上升"
    if score >= 0.30:
        return "溫和上升"
    if score <= -0.70:
        return "強勢下降"
    if score <= -0.30:
        return "溫和下降"
    return "盤整"


def classify(
    ema20: float,
    ema50: float,
    plus_di: float,
    minus_di: float,
    adx: float,
) -> tuple[int, float, str]:
    """Return ``(direction, score, label)``.

    - direction: +1 when EMA20 > EMA50 AND +DI > -DI; -1 mirrored; else 0.
    - score: direction * min(adx/50, 1.0), rounded to 4 decimals.
    - label: 5-band string via :func:`label_for`.
    """
    if ema20 > ema50 and plus_di > minus_di:
        direction = 1
    elif ema20 < ema50 and minus_di > plus_di:
        direction = -1
    else:
        direction = 0
    score = round(direction * min(adx / 50.0, 1.0), 4)
    return direction, score, label_for(score)


def _last_finite(series: pd.Series) -> float | None:
    """Return the last non-NaN/Inf value in ``series``, else None."""
    if series is None or series.empty:
        return None
    s = series.dropna()
    if s.empty:
        return None
    val = float(s.iloc[-1])
    if not math.isfinite(val):
        return None
    return val


class TrendService:
    """Consume 15m bar_close events, compute trend snapshot, persist + broadcast."""

    def __init__(
        self,
        ingest,
        indicator_cache,
        engine: AsyncEngine,
        hub,
        symbol: str = "MXF",
        resolution: str = "15m",
        min_bars: int = 60,
    ) -> None:
        self._ingest = ingest
        self._cache = indicator_cache
        self._engine = engine
        self._hub = hub
        self._symbol = symbol
        self._resolution = resolution
        self._min_bars = min_bars
        self._latest: TrendSnapshot | None = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue | None = None

    # ------------------------------------------------------------------ API

    def latest(self) -> TrendSnapshot | None:
        return self._latest

    async def get_at(self, ts: datetime) -> TrendSnapshot | None:
        """Latest persisted closed 15m row WHERE ts <= :ts.

        Never returns the in-progress bucket. If no row exists at-or-before
        (cold start), returns None.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT ts, symbol, resolution, ema20, ema50,
                           plus_di, minus_di, adx, direction, score, label
                    FROM trends
                    WHERE symbol = :symbol AND ts <= :ts
                    ORDER BY ts DESC
                    LIMIT 1
                    """
                ),
                {"symbol": self._symbol, "ts": ts},
            )
            row = result.first()
        if row is None:
            return None
        return TrendSnapshot(**dict(row._mapping))

    async def start(self) -> None:
        if self._task is not None:
            return
        self._queue = self._ingest.subscribe(self._resolution)
        self._task = asyncio.create_task(self._consume(), name="trend-service")
        log.info("TrendService started for %s @ %s", self._symbol, self._resolution)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        if self._queue is not None:
            try:
                self._ingest.unsubscribe(self._resolution, self._queue)
            except Exception:
                log.exception("TrendService unsubscribe failed")
            self._queue = None

    # ---------------------------------------------------------------- loop

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            msg = await self._queue.get()
            try:
                if msg.get("type") != "bar_close":
                    continue
                if msg.get("resolution") != self._resolution:
                    continue
                await self._on_bar_close(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TrendService _consume iteration failed")

    async def _on_bar_close(self, msg: dict) -> None:
        bucket_raw = msg.get("bucket")
        if bucket_raw is None:
            return
        try:
            bucket = (
                datetime.fromisoformat(bucket_raw)
                if isinstance(bucket_raw, str)
                else bucket_raw
            )
        except Exception:
            log.exception("TrendService: bad bucket %r", bucket_raw)
            return

        # Mirror StrategyLoop._load_bars — IngestRunner's in-memory closed-bar
        # buffer is the strategy-path source of truth and bypasses cagg lag.
        bars = self._ingest.snapshot_bars(self._resolution, limit=150)
        if bars is None or bars.empty or len(bars) < self._min_bars:
            log.debug(
                "TrendService: not enough bars (%d < %d) @ %s",
                0 if bars is None else len(bars),
                self._min_bars,
                bucket,
            )
            return

        try:
            ema20_df = self._cache.get(
                self._symbol, self._resolution, "ma", {"period": 20, "kind": "ema"}, bars
            )
            ema50_df = self._cache.get(
                self._symbol, self._resolution, "ma", {"period": 50, "kind": "ema"}, bars
            )
            dmi_df = self._cache.get(
                self._symbol, self._resolution, "dmi", {"period": 14}, bars
            )
        except Exception:
            log.exception("TrendService: indicator compute failed @ %s", bucket)
            return

        ema20 = _last_finite(ema20_df.get("ma")) if not ema20_df.empty else None
        ema50 = _last_finite(ema50_df.get("ma")) if not ema50_df.empty else None
        plus_di = _last_finite(dmi_df.get("plus_di")) if not dmi_df.empty else None
        minus_di = _last_finite(dmi_df.get("minus_di")) if not dmi_df.empty else None
        adx = _last_finite(dmi_df.get("adx")) if not dmi_df.empty else None

        if None in (ema20, ema50, plus_di, minus_di, adx):
            log.debug(
                "TrendService: NaN indicator values @ %s "
                "(ema20=%s ema50=%s +di=%s -di=%s adx=%s)",
                bucket, ema20, ema50, plus_di, minus_di, adx,
            )
            return

        direction, score, label = classify(ema20, ema50, plus_di, minus_di, adx)

        snap = TrendSnapshot(
            ts=bucket,
            symbol=self._symbol,
            resolution=self._resolution,
            ema20=round(float(ema20), 4),
            ema50=round(float(ema50), 4),
            plus_di=round(float(plus_di), 4),
            minus_di=round(float(minus_di), 4),
            adx=round(float(adx), 4),
            direction=direction,
            score=score,
            label=label,
        )

        try:
            await self._persist(snap)
        except Exception:
            log.exception("TrendService: persist failed @ %s", bucket)
            # Still update in-memory + broadcast — don't lose the signal.

        self._latest = snap

        try:
            await self._broadcast(snap)
        except Exception:
            log.exception("TrendService: broadcast failed @ %s", bucket)

    # ---------------------------------------------------------- persistence

    async def _persist(self, snap: TrendSnapshot) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO trends (
                        symbol, ts, resolution,
                        ema20, ema50, plus_di, minus_di, adx,
                        direction, score, label
                    )
                    VALUES (
                        :symbol, :ts, :resolution,
                        :ema20, :ema50, :plus_di, :minus_di, :adx,
                        :direction, :score, :label
                    )
                    ON CONFLICT (symbol, ts) DO UPDATE SET
                        resolution = EXCLUDED.resolution,
                        ema20      = EXCLUDED.ema20,
                        ema50      = EXCLUDED.ema50,
                        plus_di    = EXCLUDED.plus_di,
                        minus_di   = EXCLUDED.minus_di,
                        adx        = EXCLUDED.adx,
                        direction  = EXCLUDED.direction,
                        score      = EXCLUDED.score,
                        label      = EXCLUDED.label
                    """
                ),
                asdict(snap),
            )

    async def _broadcast(self, snap: TrendSnapshot) -> None:
        msg = {
            "type": "trend_update",
            "ts": snap.ts.isoformat(),
            "symbol": snap.symbol,
            "resolution": snap.resolution,
            "label": snap.label,
            "score": snap.score,
            "direction": snap.direction,
            "ema20": snap.ema20,
            "ema50": snap.ema50,
            "plus_di": snap.plus_di,
            "minus_di": snap.minus_di,
            "adx": snap.adx,
        }
        # NotifierHub.inapp = InAppNotifier; subscribers are async queues fed
        # via put_nowait. Push directly into the same set the WS endpoint
        # already subscribes to so frontends receive trend_update alongside
        # the existing signal stream.
        inapp = getattr(self._hub, "inapp", None)
        if inapp is None:
            return
        subs = getattr(inapp, "_subs", None)
        if not subs:
            return
        for q in list(subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("trend broadcast: subscriber queue full; dropping")
