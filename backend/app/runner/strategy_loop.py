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
from app.strategies.base import BarEvent, Signal, Strategy
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
                if msg.get("type") != "bar_close":
                    continue
                bucket = datetime.fromisoformat(msg["bucket"])
                await self._on_bar_close(resolution, bucket)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("strategy loop %s crashed", resolution)
        finally:
            self._ingest.unsubscribe(resolution, q)

    async def _on_bar_close(self, resolution: str, bucket: datetime) -> None:
        configs = await self._enabled_configs()
        candidates = [
            (cls, configs.get(cls.name))
            for cls in all_strategies().values()
            if resolution in cls.resolutions
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
