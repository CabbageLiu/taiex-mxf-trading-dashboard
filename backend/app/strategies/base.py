from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel

Side = Literal["LONG", "SHORT", "EXIT", "FLAT"]


_DAY_OPEN = time(9, 15)
_DAY_CLOSE = time(12, 15)
_NIGHT_OPEN = time(15, 0)


def in_entry_window(ts: datetime, tz: ZoneInfo) -> bool:
    """Entry-allowed iff Taipei-local time falls in
    [09:15, 12:15) ∪ [15:00, 24:00).

    Half-open intervals: at exactly 12:15:00 entries are blocked;
    at 12:14:59.999 they are allowed. Strict midnight cutoff —
    overnight 00:00–05:00 (TAIFEX night session continuation) is
    blocked per spec, even though the market is open.
    """
    t = ts.astimezone(tz).time()
    return (_DAY_OPEN <= t < _DAY_CLOSE) or (t >= _NIGHT_OPEN)


@dataclass(slots=True)
class BarEvent:
    symbol: str
    resolution: str
    bucket: datetime
    bars: pd.DataFrame
    indicators: dict[str, pd.DataFrame] = field(default_factory=dict)


@dataclass(slots=True)
class TickEvent:
    symbol: str
    resolution: str
    ts: datetime  # raw tick.ts, NOT bucket-aligned
    price: float
    bars: pd.DataFrame  # latest closed bars (same shape as on_bar)
    indicators: dict[str, pd.DataFrame] = field(default_factory=dict)


@dataclass(slots=True)
class Signal:
    ts: datetime
    symbol: str
    resolution: str
    strategy: str
    side: Side
    price: float
    reason: str = ""
    payload: dict = field(default_factory=dict)


class EmptyParams(BaseModel):
    pass


class Strategy(ABC):
    name: ClassVar[str]
    display_name: ClassVar[str | None] = None
    description: ClassVar[str | None] = None
    # Structured spec rendered as labeled rows on /analysis. Keys are zh-TW
    # row labels (e.g. "進場", "出場", "風險", "冷卻"). When set, takes
    # precedence over `description` for rendering.
    spec: ClassVar[dict[str, str] | None] = None
    resolutions: ClassVar[list[str]] = ["1m"]
    # Subset of `resolutions` that should be dispatched via `on_tick`
    # (raw `bar_update` events, ~5s cadence) rather than `on_bar` (closed
    # bucket boundaries). Default empty — preserves bar_close behaviour
    # for strategies that don't opt in.
    tick_resolutions: ClassVar[list[str]] = []
    params_schema: ClassVar[type[BaseModel]] = EmptyParams
    indicator_specs: ClassVar[dict[str, dict]] = {}
    # Auxiliary indicators computed at a different resolution from the
    # event's primary resolution (e.g. 5m MACD on a 30m strategy).
    # Each value: {"kind": str, "params": dict, "resolution": str}.
    # The framework loads aux bars + computes the indicator inline on
    # every dispatch — both `on_bar` and `on_tick` paths — and merges
    # the result into `ev.indicators` under the declared label.
    aux_indicator_specs: ClassVar[dict[str, dict]] = {}

    def __init__(self, params: BaseModel | None = None) -> None:
        self.params = params or self.params_schema()

    @abstractmethod
    def on_bar(self, ev: BarEvent) -> Signal | None: ...

    def on_tick(self, ev: TickEvent) -> Signal | None:
        """Optional opt-in hook — called per raw tick (``bar_update``).

        Default no-op. Strategies that override this method receive every
        in-progress price update for their declared resolutions, with the
        same closed-bar `bars` / `indicators` snapshot supplied to
        ``on_bar``. The override-detection branch in ``StrategyLoop`` skips
        this call for strategies that do not override the method, so the
        default path imposes zero per-tick cost.
        """
        return None

    @classmethod
    def dump_state(cls, symbol: str) -> dict:
        """Optional: return current strategy state for a given symbol.

        Strategies that hold module-level / shared state (e.g. open
        positions, daily confidence scores) override this to expose a
        snapshot via the /strategies/{name}/state endpoint. Default is
        an empty dict (stateless strategies).
        """
        return {}
