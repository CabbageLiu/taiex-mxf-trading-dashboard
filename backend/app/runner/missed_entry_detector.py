"""Periodic missed-entry detector.

Independent safety-net task that runs every ``interval_seconds`` and replays
each enabled strategy's gate evaluation against ``IngestRunner.snapshot_bars``
for that strategy's primary resolution. If the replay returns a ``Signal``
for the latest closed bucket but the live system has not persisted any
signal for that strategy in the last ``lookback_minutes``, the detector
records an alert (and, when ``MISSED_ENTRY_AUTOFIRE=true``, fires the
signal through the same hub the live loop uses).

State isolation
---------------
Strategies keep position / cooldown / latch state in module-level ``_STATE``
keyed by ``(name, symbol)``. The detector reuses the same ``_swap_state`` /
``_restore_state`` helpers the backtest engine uses (`app/backtest/engine.py`)
so each replay runs against a *clean* state slot — the live loop's state is
popped before the replay and restored after. Because Python asyncio yields
only at ``await`` points and the swap → strategy invocation → restore block
contains no awaits, the live loop cannot observe the mutated state.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import text

from app.backtest.engine import _restore_state, _swap_state
from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Signal as SignalRow
from app.indicators.service import cache as indicator_cache
from app.ingest.runner import RESOLUTION_DELTAS, IngestRunner
from app.notify.hub import NotifierHub
from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.registry import all_strategies, discover

log = logging.getLogger("taiex.detector")


def _autofire_default() -> bool:
    import os

    return os.getenv("MISSED_ENTRY_AUTOFIRE", "false").lower() in {"1", "true", "yes"}


class MissedEntryDetector:
    def __init__(
        self,
        hub: NotifierHub,
        ingest: IngestRunner,
        *,
        bar_window: int = 500,
        interval_seconds: float = 60.0,
        lookback_minutes: int = 30,
        autofire: bool | None = None,
    ) -> None:
        self._hub = hub
        self._ingest = ingest
        self._bar_window = bar_window
        self._interval = float(interval_seconds)
        self._lookback = timedelta(minutes=lookback_minutes)
        self._autofire = _autofire_default() if autofire is None else bool(autofire)
        self._symbol = get_settings().symbol_display
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Observability surface — read by /status.
        self.last_pass_ts: datetime | None = None
        self.alerts_total: int = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def autofire_enabled(self) -> bool:
        return self._autofire

    async def start(self) -> None:
        if self._task is not None:
            return
        discover()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="missed-entry-detector")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        # Wait for the runner buffer to be hot before the first pass.
        if hasattr(self._ingest, "ready") and callable(self._ingest.ready):
            await self._ingest.ready()
        # Immediate first pass — catches anything that happened during
        # startup before the next interval would have fired. Without this,
        # a missed entry in the first ``interval_seconds`` is invisible.
        if not self._stop.is_set():
            try:
                await self.run_pass()
            except Exception:
                log.exception("missed-entry detector initial pass crashed; continuing")
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self._interval)
                if self._stop.is_set():
                    break
                await self.run_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("missed-entry detector pass crashed; continuing")

    async def run_pass(self) -> None:
        """One detection pass — public so tests can drive it directly."""
        self.last_pass_ts = datetime.now(UTC)
        configs = await self._enabled_configs()
        for cls in all_strategies().values():
            cfg = configs.get(cls.name)
            if cfg is None or not cfg["enabled"]:
                continue
            try:
                await self._evaluate(cls, cfg)
            except Exception:
                log.exception("detector evaluate %s failed; continuing", cls.name)

    def _primary_resolution(self, cls: type[Strategy]) -> str | None:
        if cls.tick_resolutions:
            return cls.tick_resolutions[0]
        if cls.resolutions:
            return cls.resolutions[0]
        return None

    async def _evaluate(self, cls: type[Strategy], cfg: dict[str, Any]) -> None:
        primary = self._primary_resolution(cls)
        if primary is None:
            return
        bars = self._ingest.snapshot_bars(primary, limit=self._bar_window)
        if bars is None or bars.empty or len(bars) < 2:
            return
        bucket = self._as_pydatetime(bars.index[-1])
        if bucket is None:
            return
        # Skip if the live loop already fired a signal for this strategy
        # within the lookback window — it saw the same alignment.
        if await self._recent_signal_exists(cls.name, primary, bucket):
            return

        indicators = self._compute_indicators(cls, bars, primary)
        indicators.update(self._compute_aux_indicators(cls))

        try:
            params = cls.params_schema(**(cfg["params"] or {}))
        except Exception:
            log.exception("detector: invalid params for %s; skipping", cls.name)
            return

        ev = BarEvent(
            symbol=self._symbol,
            resolution=primary,
            bucket=bucket,
            bars=bars,
            indicators=indicators,
        )

        # SYNCHRONOUS critical section: pop live state, run strategy, restore.
        # No `await` between swap and restore — live loop cannot observe
        # the mutated state under asyncio's cooperative scheduling.
        saved = _swap_state(cls.name, self._symbol, cls)
        try:
            strat: Strategy = cls(params=params)
            signal = strat.on_bar(ev)
        except Exception:
            log.exception("detector: strategy %s on_bar raised", cls.name)
            return
        finally:
            _restore_state(saved)

        if signal is None:
            return

        self.alerts_total += 1
        log.warning(
            "missed-entry detector: %s would have fired %s @ %.2f for "
            "bucket=%s (autofire=%s)",
            cls.name,
            signal.side,
            signal.price,
            bucket.isoformat(),
            self._autofire,
        )

        if self._autofire:
            # Replay-based signals carry bucket-aligned ``ts`` + close-bar
            # ``price`` (the BarEvent path). For tick-mode strategies the
            # live loop normally fires at TICK precision — the autofire
            # signal is a degraded approximation and the resulting trade's
            # ``entry_ts`` will differ from a live entry by up to one
            # bucket. The signal payload is tagged with
            # ``source: missed_entry_detector`` so analytics can filter.
            if cls.tick_resolutions:
                log.warning(
                    "autofire: %s is tick-mode; persisted signal carries "
                    "bucket-aligned ts/price (source=missed_entry_detector)",
                    cls.name,
                )
            await self._fire(signal, cfg.get("channels", []))

    def _compute_indicators(
        self,
        cls: type[Strategy],
        bars: pd.DataFrame,
        resolution: str,
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for label, spec in cls.indicator_specs.items():
            kind = spec["kind"]
            params = spec.get("params", {})
            out[label] = indicator_cache.get(self._symbol, resolution, kind, params, bars)
        return out

    def _compute_aux_indicators(
        self, cls: type[Strategy]
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for label, spec in cls.aux_indicator_specs.items():
            res = spec["resolution"]
            kind = spec["kind"]
            params = spec.get("params", {})
            aux_bars = self._ingest.snapshot_bars(res, limit=self._bar_window)
            if aux_bars is None or aux_bars.empty:
                out[label] = pd.DataFrame()
                continue
            out[label] = indicator_cache.get(self._symbol, res, kind, params, aux_bars)
        return out

    async def _recent_signal_exists(
        self, strategy: str, resolution: str, bucket: datetime
    ) -> bool:
        """Has the live loop already fired a signal for THIS bucket?

        The dedup is bucket-bounded, not a rolling time window: a tick-mode
        strategy fires at a tick timestamp anywhere inside
        ``[bucket, bucket + delta]`` (the current open bucket while gates
        are aligned), and a bar-mode strategy fires at ``ts == bucket``.
        We allow a small slack on each side so wall-clock skew between
        the live persist and the detector lookup cannot produce a false
        negative. If ANY signal for this strategy/resolution lies in
        ``[bucket - delta, bucket + 2 * delta]``, treat as already-fired
        and skip alert.

        A rolling window (e.g. last 30 min) is unsafe: if ingest stalls
        and ``bars.index[-1]`` stays pinned on an old bucket, the live
        signal eventually rolls outside any rolling window and the
        detector spuriously re-alerts every interval — destructive when
        ``MISSED_ENTRY_AUTOFIRE`` is enabled (would persist duplicate
        signals + create duplicate trades).
        """
        if bucket.tzinfo is None:
            bucket = bucket.replace(tzinfo=UTC)
        delta = RESOLUTION_DELTAS.get(resolution)
        if delta is None:
            return False
        since = bucket - delta
        until = bucket + 2 * delta
        try:
            async with session_scope() as s:
                row = (
                    await s.execute(
                        text(
                            "SELECT 1 FROM signals "
                            "WHERE strategy = :strategy AND resolution = :resolution "
                            "AND ts >= :since AND ts < :until LIMIT 1"
                        ),
                        {
                            "strategy": strategy,
                            "resolution": resolution,
                            "since": since,
                            "until": until,
                        },
                    )
                ).first()
            return row is not None
        except Exception:
            log.exception("detector: recent-signal lookup failed; assuming none")
            return False

    async def _fire(self, signal: Signal, channels: list[str]) -> None:
        signal_id = await self._persist(signal)
        await self._hub.dispatch(signal, signal_id=signal_id, channels=channels)

    async def _persist(self, sig: Signal) -> int:
        async with session_scope() as s:
            row = SignalRow(
                ts=sig.ts,
                symbol=sig.symbol,
                resolution=sig.resolution,
                strategy=sig.strategy,
                side=sig.side,
                price=sig.price,
                payload={
                    "reason": sig.reason,
                    "fill_hint": sig.payload.get("fill_hint", "detector"),
                    "source": "missed_entry_detector",
                    **{k: v for k, v in sig.payload.items() if k != "fill_hint"},
                },
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return int(row.id)

    async def _enabled_configs(self) -> dict[str, dict[str, Any]]:
        from sqlalchemy import select

        from app.db.models import StrategyConfig

        async with session_scope() as s:
            rows = (await s.execute(select(StrategyConfig))).scalars().all()
        return {
            r.name: {"enabled": r.enabled, "params": r.params, "channels": r.channels}
            for r in rows
        }

    @staticmethod
    def _as_pydatetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        to_py = getattr(value, "to_pydatetime", None)
        if callable(to_py):
            try:
                return to_py()
            except Exception:
                return None
        return None
