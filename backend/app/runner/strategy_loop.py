from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import select

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import Signal as SignalRow
from app.db.models import StrategyConfig
from app.indicators.service import cache as indicator_cache
from app.ingest.runner import IngestRunner, RESOLUTIONS
from app.notify.hub import NotifierHub
from app.strategies.base import BarEvent, Signal, Strategy, TickEvent
from app.strategies.registry import all_strategies, discover

log = logging.getLogger("taiex.runner")


class StrategyLoop:
    def __init__(
        self,
        hub: NotifierHub,
        ingest: IngestRunner,
        bar_window: int = 500,
    ) -> None:
        self._hub = hub
        self._ingest = ingest
        self._bar_window = bar_window
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._symbol = get_settings().symbol_display

    async def start(self) -> None:
        discover()
        log.info("registered strategies: %s", list(all_strategies().keys()))
        for res in RESOLUTIONS:
            self._tasks.append(
                asyncio.create_task(self._loop(res), name=f"strategy-loop-{res}")
            )

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def _loop(self, resolution: str) -> None:
        # The entire body — including `ready()` await + `subscribe()` — is
        # wrapped in the outer crash handler so that any error during start
        # (e.g. AttributeError raised inside `ready()` itself) is logged via
        # `strategy loop %s crashed` rather than escaping silently into the
        # task. `q` is initialised to None so the `finally` unsubscribe is
        # safe even when subscribe never ran.
        q: asyncio.Queue | None = None
        try:
            # Block until the IngestRunner has hydrated its in-memory bar
            # buffer. `IngestRunner.start()` awaits hydration before
            # returning, so under normal `app/main.py` ordering this
            # resolves immediately — but the explicit await guards against
            # any future re-ordering and makes the dependency
            # self-documenting. `hasattr` (not try/except) so an internal
            # AttributeError surfaces.
            if hasattr(self._ingest, "ready") and callable(self._ingest.ready):
                await self._ingest.ready()
            q = self._ingest.subscribe(resolution)
            while not self._stop.is_set():
                # Per-iteration try/except so one bad tick / bar_close
                # cannot retire the per-resolution loop. The outer except
                # below is the safety net for non-iteration-level failures.
                try:
                    msg = await q.get()
                    msg_type = msg.get("type")
                    if msg_type == "bar_close":
                        bucket = datetime.fromisoformat(msg["bucket"])
                        await self._on_bar_close(resolution, bucket)
                    elif msg_type == "bar_update":
                        ts = datetime.fromisoformat(msg["ts"])
                        price = float(msg["price"])
                        await self._on_tick(resolution, ts, price)
                    else:
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "strategy loop %s iteration error; continuing",
                        resolution,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("strategy loop %s crashed", resolution)
        finally:
            if q is not None:
                self._ingest.unsubscribe(resolution, q)

    async def _on_bar_close(self, resolution: str, bucket: datetime) -> None:
        # Tick-mode strategies (those that declare ``resolution`` in
        # ``tick_resolutions``) ALSO receive bar_close dispatch — paired
        # with the in-memory bar buffer in ``IngestRunner`` (which appends
        # the just-closed bucket to its deque inside ``_emit_close`` BEFORE
        # this fan-out fires), bar_close gives a deterministic per-bucket
        # evaluation point that does not depend on cagg refresh latency.
        # Position-based dedupe (``st.position`` short-circuit in
        # ``_evaluate``) prevents double-fire when a tick arrives shortly
        # after this bar_close.
        configs = await self._enabled_configs()
        candidates = [
            (cls, configs.get(cls.name))
            for cls in all_strategies().values()
            if resolution in cls.resolutions
        ]
        if not candidates:
            return

        for cls, cfg in candidates:
            if cfg is None or not cfg["enabled"]:
                continue
            try:
                params = cls.params_schema(**(cfg["params"] or {}))
                bars = await self._load_bars(resolution)
                if bars.empty:
                    continue
                indicators = self._compute_indicators(cls, bars, resolution)
                indicators.update(await self._compute_aux_indicators(cls))
                ev = BarEvent(
                    symbol=self._symbol,
                    resolution=resolution,
                    bucket=bucket,
                    bars=bars,
                    indicators=indicators,
                )
                strat: Strategy = cls(params=params)
                signal = strat.on_bar(ev)
            except Exception:
                log.exception("strategy %s on_bar dispatch failed", cls.name)
                continue
            if signal is None:
                continue
            await self._fire(signal, cfg["channels"])

    async def _on_tick(self, resolution: str, ts: datetime, price: float) -> None:
        # Opt-in by `tick_resolutions` AND `on_tick` override (defence-in-depth:
        # a strategy that wires `tick_resolutions` without overriding `on_tick`
        # is treated as inert rather than dispatching to the no-op default).
        candidates = [
            cls
            for cls in all_strategies().values()
            if resolution in cls.tick_resolutions
            and cls.on_tick is not Strategy.on_tick
        ]
        if not candidates:
            return
        configs = await self._enabled_configs()

        for cls in candidates:
            cfg = configs.get(cls.name)
            if cfg is None or not cfg["enabled"]:
                continue
            try:
                params = cls.params_schema(**(cfg["params"] or {}))
                bars = await self._load_bars(resolution)
                if bars.empty:
                    continue
                indicators = self._compute_indicators(cls, bars, resolution)
                indicators.update(await self._compute_aux_indicators(cls))
                ev = TickEvent(
                    symbol=self._symbol,
                    resolution=resolution,
                    ts=ts,
                    price=price,
                    bars=bars,
                    indicators=indicators,
                )
                strat: Strategy = cls(params=params)
                signal = strat.on_tick(ev)
            except Exception:
                log.exception("strategy %s on_tick dispatch failed", cls.name)
                continue
            if signal is None:
                continue
            await self._fire(signal, cfg["channels"])

    async def _compute_aux_indicators(
        self, cls: type[Strategy]
    ) -> dict[str, pd.DataFrame]:
        """Load bars + compute indicators at non-primary resolutions.

        For each entry in `cls.aux_indicator_specs`, fetch bars at that
        resolution via the same `_load_bars` path the primary side uses,
        then run `indicator_cache.get`. Empty bars yield an empty DataFrame
        under the label so strategies see a uniform shape.
        """
        out: dict[str, pd.DataFrame] = {}
        for label, spec in cls.aux_indicator_specs.items():
            res = spec["resolution"]
            kind = spec["kind"]
            params = spec.get("params", {})
            aux_bars = await self._load_bars(res)
            if aux_bars.empty:
                out[label] = pd.DataFrame()
                continue
            out[label] = indicator_cache.get(self._symbol, res, kind, params, aux_bars)
        return out

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
                payload={"reason": sig.reason, **sig.payload},
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return int(row.id)

    async def _enabled_configs(self) -> dict[str, dict]:
        async with session_scope() as s:
            rows = (await s.execute(select(StrategyConfig))).scalars().all()
        return {
            r.name: {"enabled": r.enabled, "params": r.params, "channels": r.channels}
            for r in rows
        }

    async def _load_bars(self, resolution: str) -> pd.DataFrame:
        # Strategy-path source of truth: IngestRunner's in-memory closed-bar
        # buffer. Bypasses cagg refresh lag at bucket boundaries. Cagg
        # remains source of truth for /bars REST + UI + backtest.
        return self._ingest.snapshot_bars(resolution, limit=self._bar_window)

    def _compute_indicators(
        self, cls: type[Strategy], bars: pd.DataFrame, resolution: str
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for label, spec in cls.indicator_specs.items():
            kind = spec["kind"]
            params = spec.get("params", {})
            out[label] = indicator_cache.get(self._symbol, resolution, kind, params, bars)
        return out
