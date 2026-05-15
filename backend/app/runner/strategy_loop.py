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
        q = self._ingest.subscribe(resolution)
        try:
            while not self._stop.is_set():
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
            log.exception("strategy loop %s crashed", resolution)
        finally:
            self._ingest.unsubscribe(resolution, q)

    async def _on_bar_close(self, resolution: str, bucket: datetime) -> None:
        configs = await self._enabled_configs()
        # Tick-routed resolutions are excluded so the on_bar shim never
        # fires ahead of the on_tick dispatch (ingest queues bar_close
        # before bar_update on a boundary tick). Bar-close path still
        # delivers events for resolutions a strategy declares but does
        # NOT include in `tick_resolutions` (e.g. backtest path, or
        # auxiliary subscriptions if any strategy ever needs them).
        candidates = [
            (cls, configs.get(cls.name))
            for cls in all_strategies().values()
            if resolution in cls.resolutions
            and resolution not in cls.tick_resolutions
        ]
        if not candidates:
            return
        bars = await self._load_bars(resolution)
        if bars.empty:
            return

        for cls, cfg in candidates:
            if cfg is None or not cfg["enabled"]:
                continue
            try:
                params = cls.params_schema(**(cfg["params"] or {}))
            except Exception:
                log.exception("invalid params for %s; skipping", cls.name)
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
            try:
                strat: Strategy = cls(params=params)
                signal = strat.on_bar(ev)
            except Exception:
                log.exception("strategy %s on_bar raised", cls.name)
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
        bars = await self._load_bars(resolution)
        if bars.empty:
            return

        for cls in candidates:
            cfg = configs.get(cls.name)
            if cfg is None or not cfg["enabled"]:
                continue
            try:
                params = cls.params_schema(**(cfg["params"] or {}))
            except Exception:
                log.exception("invalid params for %s; skipping", cls.name)
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
            try:
                strat: Strategy = cls(params=params)
                signal = strat.on_tick(ev)
            except Exception:
                log.exception("strategy %s on_tick raised", cls.name)
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
        from app.api.routes.bars import load_bars

        return await load_bars(self._symbol, resolution, limit=self._bar_window)

    def _compute_indicators(
        self, cls: type[Strategy], bars: pd.DataFrame, resolution: str
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for label, spec in cls.indicator_specs.items():
            kind = spec["kind"]
            params = spec.get("params", {})
            out[label] = indicator_cache.get(self._symbol, resolution, kind, params, bars)
        return out
