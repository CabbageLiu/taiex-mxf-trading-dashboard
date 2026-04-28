"""Smoke-test strategy. Emits LONG on every bar close. Drop-in proof that the
plug-in pipeline (registry → runner → notifier hub → DB → WS) is wired."""

from __future__ import annotations

from pydantic import BaseModel

from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.registry import register_strategy


class _Params(BaseModel):
    pass


@register_strategy
class AlwaysLong(Strategy):
    name = "always_long"
    resolutions = ["1m", "5m"]
    params_schema = _Params

    def on_bar(self, ev: BarEvent) -> Signal | None:
        if ev.bars.empty:
            return None
        last = ev.bars.iloc[-1]
        return Signal(
            ts=ev.bucket,
            symbol=ev.symbol,
            resolution=ev.resolution,
            strategy=self.name,
            side="LONG",
            price=float(last["close"]),
            reason="always-long smoke test",
        )
