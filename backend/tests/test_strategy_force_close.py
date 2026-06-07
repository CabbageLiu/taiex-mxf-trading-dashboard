"""End-of-window (EOW) force-close tests for the live tick-driven strategies.

Each of `strat_30k`, `strat_15k`, `strat_1k` must close any open position
the moment the evaluation timestamp falls outside its entry window — even
without an explicit TP/SL/TRAIL trigger. The shared insertion at the top
of each strategy's `_evaluate` helper enforces this; these tests pin one
boundary per strategy.

Boundaries covered:
  strat_30k — day-session close at 12:15 Taipei
  strat_15k — night-window midnight cutoff at 24:00 Taipei
  strat_1k  — day-session close at 13:45 Taipei
  strat_1k  — overnight close at 05:00 Taipei

The test uses a tick *just inside* the window to seed an open position
(via direct state injection — bypasses entry gates), then fires a tick
*just outside* the window and asserts an EXIT Signal with reason="EOW"
is returned + `state.position is None` after.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.strategies.base import TickEvent
from app.strategies.examples import strat_1k as mod_1k
from app.strategies.examples import strat_15k as mod_15k
from app.strategies.examples import strat_30k as mod_30k

TPE = ZoneInfo("Asia/Taipei")
SYM = "MXF"


def _tpe(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TPE)


def _empty_bars() -> pd.DataFrame:
    """Empty OHLC frame — EOW path doesn't consult bars, just a sentinel."""
    return pd.DataFrame(
        {
            "open": pd.Series(dtype=float),
            "high": pd.Series(dtype=float),
            "low": pd.Series(dtype=float),
            "close": pd.Series(dtype=float),
            "tick_count": pd.Series(dtype=int),
        }
    )


@pytest.fixture(autouse=True)
def _reset_state():
    mod_30k._STATE.clear()
    mod_15k._STATE.clear()
    mod_1k._STATE.clear()
    yield
    mod_30k._STATE.clear()
    mod_15k._STATE.clear()
    mod_1k._STATE.clear()


# ---------------------------------------------------------------------------
# strat_30k — day-session close at 12:15 Taipei
# ---------------------------------------------------------------------------


def test_strat_30k_force_close_at_1215():
    """Open LONG seeded at 11:50 Taipei. Tick at 12:16 Taipei → EOW EXIT."""
    strat = mod_30k.TradeStrat30K()
    entry_ts = _tpe(2026, 5, 1, 11, 50)
    entry_price = 20_000.0

    state = mod_30k._state_for(strat.name, SYM)
    state.position = mod_30k._PositionState(
        side="LONG",
        entry_price=entry_price,
        entry_ts=entry_ts,
        entry_ind={
            "k": 60.0, "d": 50.0, "macd": 1.0, "signal": 0.5,
            "hist": 0.5, "plus_di": 25.0, "minus_di": 18.0, "adx": 24.0,
        },
        peak_pnl=0.0,
    )

    tick_ts = _tpe(2026, 5, 1, 12, 16)  # outside [09:10, 12:15)
    tick_price = 20_050.0
    ev = TickEvent(
        symbol=SYM,
        resolution="30m",
        ts=tick_ts,
        price=tick_price,
        bars=_empty_bars(),
        indicators={},
    )

    sig = strat.on_tick(ev)

    assert sig is not None, "expected EOW exit signal"
    assert sig.side == "EXIT"
    assert sig.reason == "EOW"
    assert sig.strategy == strat.name
    assert sig.symbol == SYM
    assert sig.resolution == "30m"
    assert sig.price == tick_price
    assert sig.payload["exit_reason"] == "EOW"
    assert sig.payload["entry_side"] == "LONG"
    assert sig.payload["entry_price"] == entry_price
    assert sig.payload["pnl_points"] == pytest.approx(50.0)
    assert sig.payload["fill_hint"] == "tick"
    assert state.position is None


# ---------------------------------------------------------------------------
# strat_15k — night-window midnight cutoff at 24:00 Taipei
# ---------------------------------------------------------------------------


def test_strat_15k_force_close_at_2400():
    """Open LONG seeded at 23:55 Taipei. Tick at 00:01 Taipei → EOW EXIT.

    `in_entry_window` with `night_close=None` uses the legacy strict-midnight
    cutoff (block 00:00 onwards), so 00:01 is outside the window.
    """
    strat = mod_15k.TradeStrat15K()
    entry_ts = _tpe(2026, 5, 1, 23, 55)
    entry_price = 20_000.0

    state = mod_15k._state_for(strat.name, SYM)
    state.position = mod_15k._PositionState(
        side="LONG",
        entry_price=entry_price,
        entry_ts=entry_ts,
        entry_ind={
            "k": 55.0, "d": 50.0, "macd": 0.8, "signal": 0.2,
            "hist": 0.6, "plus_di": 26.0, "minus_di": 17.0, "adx": 23.0,
        },
        peak_pnl=0.0,
    )

    tick_ts = _tpe(2026, 5, 2, 0, 1)  # past 24:00 Taipei → outside window
    tick_price = 19_980.0
    ev = TickEvent(
        symbol=SYM,
        resolution="15m",
        ts=tick_ts,
        price=tick_price,
        bars=_empty_bars(),
        indicators={},
    )

    sig = strat.on_tick(ev)

    assert sig is not None, "expected EOW exit signal"
    assert sig.side == "EXIT"
    assert sig.reason == "EOW"
    assert sig.strategy == strat.name
    assert sig.resolution == "15m"
    assert sig.price == tick_price
    assert sig.payload["exit_reason"] == "EOW"
    assert sig.payload["entry_side"] == "LONG"
    assert sig.payload["entry_price"] == entry_price
    # 19_980 - 20_000 = -20
    assert sig.payload["pnl_points"] == pytest.approx(-20.0)
    assert state.position is None


# ---------------------------------------------------------------------------
# strat_1k — day-session close at 13:45 Taipei
# ---------------------------------------------------------------------------


def test_strat_1k_force_close_at_1345():
    """Open LONG seeded at 13:30 Taipei. Tick at 13:46 Taipei → EOW EXIT."""
    strat = mod_1k.TradeStrat1K()
    entry_ts = _tpe(2026, 5, 1, 13, 30)
    entry_price = 20_000.0

    state = mod_1k._state_for(strat.name, SYM)
    state.position = mod_1k._PositionState(
        side="LONG",
        entry_price=entry_price,
        entry_ts=entry_ts,
        entry_ind={
            "k": 62.0, "d": 55.0, "macd": 0.5, "signal": 0.1,
            "hist": 0.4, "plus_di": 27.0, "minus_di": 14.0, "adx": 22.0,
        },
        peak_pnl=0.0,
    )

    tick_ts = _tpe(2026, 5, 1, 13, 46)  # past 13:45 day-close
    tick_price = 20_010.0
    ev = TickEvent(
        symbol=SYM,
        resolution="1m",
        ts=tick_ts,
        price=tick_price,
        bars=_empty_bars(),
        indicators={},
    )

    sig = strat.on_tick(ev)

    assert sig is not None, "expected EOW exit signal"
    assert sig.side == "EXIT"
    assert sig.reason == "EOW"
    assert sig.strategy == strat.name
    assert sig.resolution == "1m"
    assert sig.price == tick_price
    assert sig.payload["exit_reason"] == "EOW"
    assert sig.payload["entry_side"] == "LONG"
    assert sig.payload["entry_price"] == entry_price
    assert sig.payload["pnl_points"] == pytest.approx(10.0)
    assert state.position is None


# ---------------------------------------------------------------------------
# strat_1k — overnight close at 05:00 Taipei
# ---------------------------------------------------------------------------


def test_strat_1k_force_close_at_0500():
    """Open SHORT seeded at 04:55 Taipei. Tick at 05:01 Taipei → EOW EXIT.

    Verifies the overnight-wrap upper bound (`night_close=05:00`, exclusive)
    is honoured: 05:01 is outside the window even though the night session
    technically extends past midnight in TAIFEX. SHORT pnl = entry - exit
    so a +20 move against the position yields -20 pnl_points.
    """
    strat = mod_1k.TradeStrat1K()
    entry_ts = _tpe(2026, 5, 1, 4, 55)
    entry_price = 20_000.0

    state = mod_1k._state_for(strat.name, SYM)
    state.position = mod_1k._PositionState(
        side="SHORT",
        entry_price=entry_price,
        entry_ts=entry_ts,
        entry_ind={
            "k": 30.0, "d": 40.0, "macd": -0.5, "signal": -0.1,
            "hist": -0.4, "plus_di": 12.0, "minus_di": 26.0, "adx": 20.0,
        },
        peak_pnl=0.0,
    )

    tick_ts = _tpe(2026, 5, 1, 5, 1)  # past 05:00 overnight close
    tick_price = 20_020.0
    ev = TickEvent(
        symbol=SYM,
        resolution="1m",
        ts=tick_ts,
        price=tick_price,
        bars=_empty_bars(),
        indicators={},
    )

    sig = strat.on_tick(ev)

    assert sig is not None, "expected EOW exit signal"
    assert sig.side == "EXIT"
    assert sig.reason == "EOW"
    assert sig.strategy == strat.name
    assert sig.resolution == "1m"
    assert sig.price == tick_price
    assert sig.payload["exit_reason"] == "EOW"
    assert sig.payload["entry_side"] == "SHORT"
    assert sig.payload["entry_price"] == entry_price
    # SHORT: entry - exit = 20000 - 20020 = -20
    assert sig.payload["pnl_points"] == pytest.approx(-20.0)
    assert state.position is None
