from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BarEvent
from app.strategies.examples import trade_strat_v1 as mod
from app.strategies.examples.trade_strat_v1 import (
    _STATE,
    TradeStratV1,
    TradeStratV1Params,
    _PositionState,
)


@pytest.fixture(autouse=True)
def reset_state():
    _STATE.clear()
    yield
    _STATE.clear()


def _bars(closes: list[float], freq: str = "30min") -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2026-04-29", periods=n, freq=freq, tz="UTC")
    arr = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": arr,
            "high": arr,
            "low": arr,
            "close": arr,
            "tick_count": np.full(n, 1, dtype=int),
        },
        index=idx,
    )


def _ind(
    period: int,
    plus: float,
    minus: float,
    k: float = 50.0,
    d: float = 50.0,
    macd_tail: tuple[float, float, float] = (-0.10, 0.05, 0.12),
    freq: str = "30min",
) -> dict[str, pd.DataFrame]:
    """Build KD/MACD/DMI fixtures.

    ``macd_tail`` controls the last 3 macd values. Default satisfies the
    rising-edge gate (was non-positive, became positive, kept rising) so
    the 30m entry path can fire.
    """
    idx = pd.date_range("2026-04-29", periods=period + 1, freq=freq, tz="UTC")
    n = len(idx)
    macd_arr = np.full(n, -0.5, dtype=float)  # priors strongly non-positive
    if n >= 3:
        macd_arr[-3] = macd_tail[0]
        macd_arr[-2] = macd_tail[1]
        macd_arr[-1] = macd_tail[2]
    return {
        "kd": pd.DataFrame(
            {"k": np.full(n, k), "d": np.full(n, d)}, index=idx
        ),
        "macd": pd.DataFrame(
            {
                "macd": macd_arr,
                "signal": np.zeros(n),
                "hist": macd_arr.copy(),
            },
            index=idx,
        ),
        "dmi": pd.DataFrame(
            {
                "plus_di": np.full(n, plus),
                "minus_di": np.full(n, minus),
                "adx": np.full(n, 25.0),
            },
            index=idx,
        ),
    }


def _open_long(
    strat: TradeStratV1, entry_price: float = 39300.0
) -> _PositionState:
    """Helper: drive a 30m rising-edge bar to open a LONG, return position."""
    bars = _bars([entry_price - 300, entry_price - 200, entry_price - 100, entry_price])
    indicators = _ind(period=14, plus=25.0, minus=10.0)
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )
    sig = strat.on_bar(ev)
    assert sig is not None and sig.side == "LONG"
    state = mod._state_for(TradeStratV1.name, "MXF")
    assert state.position is not None
    return state.position


def _force_open_short(entry_price: float = 39300.0) -> None:
    """Helper: directly seed a SHORT position into module state.

    Used by tests that need to exercise SHORT exit paths without going
    through the (default-disabled) `enable_short` entry gate.
    """
    st = mod._state_for(TradeStratV1.name, "MXF")
    st.position = _PositionState(
        side="SHORT",
        entry_price=entry_price,
        entry_ts=datetime(2026, 4, 29, 5, 30, tzinfo=UTC),
    )


def test_dump_state_empty_when_no_activity():
    assert TradeStratV1.dump_state("MXF") == {}


def test_dump_state_after_position_open():
    key = (TradeStratV1.name, "MXF")
    st = mod._state_for(TradeStratV1.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=39400.0,
        entry_ts=datetime(2026, 4, 29, 5, 30, tzinfo=UTC),
    )
    st.daily_confidence_long = 2
    st.daily_confidence_short = 1
    st.cooldown_left = 0

    snap = TradeStratV1.dump_state("MXF")
    assert snap["daily_confidence_long"] == 2
    assert snap["daily_confidence_short"] == 1
    assert snap["cooldown_left"] == 0
    assert snap["position"] == {
        "side": "LONG",
        "entry_price": 39400.0,
        "entry_ts": "2026-04-29T05:30:00+00:00",
    }
    assert key in _STATE


def test_long_entry_on_rising_edge():
    strat = TradeStratV1(params=TradeStratV1Params())
    bars = _bars([39000, 39100, 39200, 39300])
    indicators = _ind(period=14, plus=25.0, minus=10.0)
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = strat.on_bar(ev)

    assert sig is not None
    assert sig.side == "LONG"
    assert sig.price == 39300
    state = mod._state_for(TradeStratV1.name, "MXF")
    assert state.position is not None
    assert state.position.entry_price == 39300


def test_long_entry_does_not_repeat_without_reset():
    strat = TradeStratV1(params=TradeStratV1Params())
    bars = _bars([39000, 39100, 39200, 39300])
    indicators = _ind(period=14, plus=25.0, minus=10.0)
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    first = strat.on_bar(ev)
    assert first is not None and first.side == "LONG"

    # Same conditions on next bar: no rising edge, no fresh entry.
    second = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)
    assert second is None


def test_daily_confidence_updates_long_score():
    strat = TradeStratV1(params=TradeStratV1Params())
    # K, D = 85 → above the short_ceiling (80), so the short side gets 0 on KD.
    # plus_di > 21, minus_di < 21, macd > 0 → long gets 3/3, short gets 0/3.
    indicators = _ind(period=14, plus=25.0, minus=10.0, k=85.0, d=85.0)
    bars = _bars([39000, 39100, 39200, 39300])
    ev = BarEvent(
        symbol="MXF",
        resolution="1d",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = strat.on_bar(ev)

    assert sig is None  # daily layer never emits signals
    state = mod._state_for(TradeStratV1.name, "MXF")
    assert state.daily_confidence_long == 3
    assert state.daily_confidence_short == 0


def test_macd_rising_edge_positive():
    """Last 3 macd = [-0.1, 0.05, 0.12] satisfies the rising-edge gate
    when the other entry conditions hold."""
    strat = TradeStratV1(params=TradeStratV1Params())
    bars = _bars([39000, 39100, 39200, 39300])
    indicators = _ind(
        period=14,
        plus=25.0,
        minus=10.0,
        macd_tail=(-0.1, 0.05, 0.12),
    )
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = strat.on_bar(ev)

    assert sig is not None
    assert sig.side == "LONG"


@pytest.mark.parametrize(
    "tail",
    [
        (0.05, 0.10, 0.20),   # already positive 3 bars ago
        (-0.1, -0.05, 0.02),  # only just-turned-positive on last bar
        (-0.1, 0.05, 0.03),   # turned positive but ticked down
    ],
)
def test_macd_not_rising_no_entry(tail):
    strat = TradeStratV1(params=TradeStratV1Params())
    bars = _bars([39000, 39100, 39200, 39300])
    indicators = _ind(
        period=14,
        plus=25.0,
        minus=10.0,
        macd_tail=tail,
    )
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = strat.on_bar(ev)

    assert sig is None


def test_open_position_payload_carries_entry_ind():
    strat = TradeStratV1(params=TradeStratV1Params())
    bars = _bars([39000, 39100, 39200, 39300])
    indicators = _ind(period=14, plus=25.0, minus=10.0)
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = strat.on_bar(ev)

    assert sig is not None
    assert "entry_ind" in sig.payload
    expected_keys = {"k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx"}
    assert set(sig.payload["entry_ind"].keys()) == expected_keys


def test_close_position_payload_carries_exit_ind():
    """Open a LONG, then push close +251 pts on the next 30m bar to TP."""
    strat = TradeStratV1(params=TradeStratV1Params())

    entry_bars = _bars([39000, 39100, 39200, 39300])
    entry_ind = _ind(period=14, plus=25.0, minus=10.0)
    entry_ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=entry_bars.index[-1].to_pydatetime(),
        bars=entry_bars,
        indicators=entry_ind,
    )
    open_sig = strat.on_bar(entry_ev)
    assert open_sig is not None and open_sig.side == "LONG"
    entry_price = open_sig.price

    # TP exit: close jumps +251 pts (above the 250-pt TP threshold).
    # Reuse same indicator fixture (still passes the gates, but exit
    # check fires first inside _on_30m).
    tp_close = entry_price + 251.0
    exit_bars = _bars([39000, 39100, 39200, tp_close])
    exit_ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=exit_bars.index[-1].to_pydatetime(),
        bars=exit_bars,
        indicators=entry_ind,
    )
    exit_sig = TradeStratV1(params=TradeStratV1Params()).on_bar(exit_ev)

    assert exit_sig is not None
    assert exit_sig.side == "EXIT"
    assert exit_sig.payload.get("exit_reason") == "TP"
    assert "exit_ind" in exit_sig.payload
    expected_keys = {"k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx"}
    assert set(exit_sig.payload["exit_ind"].keys()) == expected_keys


# ─── V5.1 — new exit-rules suite ───────────────────────────────────────────


def test_v1_tp_at_250():
    """LONG opened at 17000; 30m bar close at 17251 → TP fires (>=250)."""
    # Seed an explicit entry price of 17000 so the +251 → TP threshold
    # is unambiguous and independent of `_open_long`'s entry.
    st = mod._state_for(TradeStratV1.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=17000.0,
        entry_ts=datetime(2026, 4, 29, 5, 30, tzinfo=UTC),
    )

    # Use indicator fixture that does NOT trigger MACD-falling (rising).
    indicators = _ind(period=14, plus=25.0, minus=10.0,
                      macd_tail=(-0.1, 0.05, 0.12))
    bars = _bars([17000, 17050, 17100, 17251])
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload.get("exit_reason") == "TP"
    assert sig.payload.get("pnl_points") >= 250.0


def test_v1_no_tp_below_250():
    """LONG opened at 17000; 30m bar close at 17249 → no exit."""
    st = mod._state_for(TradeStratV1.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=17000.0,
        entry_ts=datetime(2026, 4, 29, 5, 30, tzinfo=UTC),
    )

    # MACD rising so the falling-MACD exit is not tripped either.
    indicators = _ind(period=14, plus=25.0, minus=10.0,
                      macd_tail=(-0.1, 0.05, 0.12))
    bars = _bars([17000, 17050, 17100, 17249])
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)

    assert sig is None


def test_v1_10m_di_flip_closes_long():
    """LONG open; 10m bar with -DI=25 > +DI=20 → DI_FLIP_10M EXIT."""
    strat = TradeStratV1(params=TradeStratV1Params())
    pos = _open_long(strat)

    # Build a 10m bar event with -DI > +DI. The 10m exit_assist path
    # only consumes DMI + close, so KD/MACD shapes don't matter.
    bars = _bars([pos.entry_price] * 4, freq="10min")
    indicators = _ind(period=14, plus=20.0, minus=25.0, freq="10min")
    ev = BarEvent(
        symbol="MXF",
        resolution="10m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload.get("exit_reason") == "DI_FLIP_10M"


def test_v1_10m_di_flip_does_not_fire_when_long_dominant():
    """LONG open; 10m bar with -DI=20 < +DI=25 → no exit."""
    strat = TradeStratV1(params=TradeStratV1Params())
    pos = _open_long(strat)

    bars = _bars([pos.entry_price] * 4, freq="10min")
    indicators = _ind(period=14, plus=25.0, minus=20.0, freq="10min")
    ev = BarEvent(
        symbol="MXF",
        resolution="10m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)

    assert sig is None


def test_v1_10m_di_flip_closes_short():
    """SHORT open; 10m bar with +DI=25 > -DI=20 → DI_FLIP_10M EXIT."""
    _force_open_short(entry_price=17000.0)

    bars = _bars([17000.0] * 4, freq="10min")
    indicators = _ind(period=14, plus=25.0, minus=20.0, freq="10min")
    ev = BarEvent(
        symbol="MXF",
        resolution="10m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload.get("exit_reason") == "DI_FLIP_10M"


def test_v1_30m_macd_falling_closes_long():
    """LONG open; 30m bar with macd[-2]=0.6, macd[-1]=0.4 → MACD_DOWN_30M."""
    strat = TradeStratV1(params=TradeStratV1Params())
    pos = _open_long(strat)

    # Hold price flat so neither TP nor SL trips. macd_tail tuple
    # places (m[-3], m[-2], m[-1]) = (0.05, 0.6, 0.4) — falling on the
    # most recent bar.
    bars = _bars([pos.entry_price] * 4)
    indicators = _ind(
        period=14, plus=25.0, minus=10.0, macd_tail=(0.05, 0.6, 0.4)
    )
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload.get("exit_reason") == "MACD_DOWN_30M"


def test_v1_30m_macd_rising_does_not_close_long():
    """LONG open; 30m bar with macd[-2]=0.4, macd[-1]=0.6 → no exit."""
    strat = TradeStratV1(params=TradeStratV1Params())
    pos = _open_long(strat)

    bars = _bars([pos.entry_price] * 4)
    indicators = _ind(
        period=14, plus=25.0, minus=10.0, macd_tail=(0.05, 0.4, 0.6)
    )
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)

    assert sig is None


def test_v1_3m_no_longer_dispatches():
    """3m bar (with open position, any DMI shape) → on_bar returns None."""
    strat = TradeStratV1(params=TradeStratV1Params())
    _open_long(strat)

    bars = _bars([39300.0] * 4, freq="3min")
    # Strong -DI flip that *would* have triggered the old V1 3m exit.
    indicators = _ind(period=14, plus=10.0, minus=30.0, freq="3min")
    ev = BarEvent(
        symbol="MXF",
        resolution="3m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)

    assert sig is None


def test_v1_tp_priority_over_macd_falling():
    """Bar that simultaneously hits TP (+251) AND falling MACD → TP wins."""
    st = mod._state_for(TradeStratV1.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=17000.0,
        entry_ts=datetime(2026, 4, 29, 5, 30, tzinfo=UTC),
    )

    # close 17251 (+251 → TP) AND macd falling (0.6 → 0.4).
    bars = _bars([17000, 17050, 17100, 17251])
    indicators = _ind(
        period=14, plus=25.0, minus=10.0, macd_tail=(0.05, 0.6, 0.4)
    )
    ev = BarEvent(
        symbol="MXF",
        resolution="30m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = TradeStratV1(params=TradeStratV1Params()).on_bar(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload.get("exit_reason") == "TP"
