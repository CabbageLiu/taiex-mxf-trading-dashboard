from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel

Side = Literal["LONG", "SHORT", "EXIT", "FLAT"]


_DEFAULT_DAY_OPEN = time(9, 15)
_DEFAULT_DAY_CLOSE = time(12, 15)
_DEFAULT_NIGHT_OPEN = time(21, 0)


def in_entry_window(
    ts: datetime,
    tz: ZoneInfo,
    *,
    day_open: time = _DEFAULT_DAY_OPEN,
    day_close: time = _DEFAULT_DAY_CLOSE,
    night_open: time = _DEFAULT_NIGHT_OPEN,
    night_close: time | None = None,
) -> bool:
    """Entry-allowed iff local-tz time falls in
    [day_open, day_close) ∪ <night-window>.

    Day half-open: ``day_open <= t < day_close``.

    Night half-open semantics depend on ``night_close``:
      - ``None`` (default): ``t >= night_open``. Strict midnight cutoff —
        00:00 onward is blocked. Preserves legacy behaviour.
      - ``night_close > night_open``: same-day ``[night_open, night_close)``.
      - ``night_close < night_open``: overnight wrap —
        ``t >= night_open OR t < night_close`` (upper bound exclusive).
      - ``night_close == night_open``: empty night window.
    """
    t = ts.astimezone(tz).time()
    in_day = day_open <= t < day_close
    if night_close is None:
        in_night = t >= night_open
    elif night_close > night_open:
        in_night = night_open <= t < night_close
    elif night_close < night_open:
        in_night = t >= night_open or t < night_close
    else:
        in_night = False
    return in_day or in_night


def in_market_session(
    ts: datetime,
    tz: ZoneInfo,
    *,
    day_open: time,
    day_close: time,
    night_open: time,
    night_close: time,
) -> bool:
    """True iff ``ts`` falls inside an open TAIFEX trading session.

    This is the *full* market session (day 08:45–13:45, night 15:00→05:00),
    NOT the narrower entry window in :func:`in_entry_window`. The feed-health
    watchdog uses it so it only forces reconnects while the feed should be
    delivering ticks — the exchange emits nothing in the closed gaps
    (13:45–15:00, 05:00–08:45) or at weekends, so silence there is normal.

    Weekday rules (assumes the standard overnight wrap ``night_close <
    night_open``):
      - Day session: Mon–Fri, ``day_open <= t < day_close``.
      - Night session opens Mon–Fri at ``night_open`` and runs to ``night_close``
        the next morning. The evening leg (``t >= night_open``) is Mon–Fri; the
        overnight tail (``t < night_close``) belongs to the prior day's open, so
        it is valid Tue–Sat. Saturday after ``night_close`` and all of Sunday
        are closed.
    """
    local = ts.astimezone(tz)
    t = local.time()
    wd = local.weekday()  # Mon=0 .. Sun=6
    if wd <= 4 and day_open <= t < day_close:
        return True
    if wd <= 4 and t >= night_open:
        return True
    if 1 <= wd <= 5 and t < night_close:
        return True
    return False


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
    # One-line zh-TW summary of the entry gates, rendered in the Discord
    # open-signal embed. Indicator names stay English. Falls back to a
    # generic phrase when unset.
    entry_summary_tc: ClassVar[str | None] = None
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
