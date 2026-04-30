from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BarEvent
from app.strategies.examples import trade_strat_v2 as mod
from app.strategies.examples.trade_strat_v2 import (
    _STATE,
    TradeStratV2,
    TradeStratV2Params,
    _PositionState,
)


@pytest.fixture(autouse=True)
def reset_state():
    _STATE.clear()
    yield
    _STATE.clear()


def _bars(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2026-04-29", periods=n, freq="10min", tz="UTC")
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
    period: int, plus: float, minus: float, k: float = 50.0, d: float = 50.0
) -> dict[str, pd.DataFrame]:
    idx = pd.date_range("2026-04-29", periods=period + 1, freq="10min", tz="UTC")
    n = len(idx)
    return {
        "kd": pd.DataFrame(
            {"k": np.full(n, k), "d": np.full(n, d)}, index=idx
        ),
        "macd": pd.DataFrame(
            {"macd": np.full(n, 1.0), "signal": np.zeros(n), "hist": np.full(n, 1.0)},
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


def test_v2_dump_state_empty_when_no_activity():
    assert TradeStratV2.dump_state("MXF") == {}


def test_v2_dump_state_after_position_open():
    key = (TradeStratV2.name, "MXF")
    st = mod._state_for(TradeStratV2.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=39400.0,
        entry_ts=datetime(2026, 4, 29, 5, 30, tzinfo=UTC),
    )
    st.daily_confidence_long = 2
    st.daily_confidence_short = 1
    st.cooldown_left = 0

    snap = TradeStratV2.dump_state("MXF")
    assert snap["daily_confidence_long"] == 2
    assert snap["daily_confidence_short"] == 1
    assert snap["cooldown_left"] == 0
    assert snap["position"] == {
        "side": "LONG",
        "entry_price": 39400.0,
        "entry_ts": "2026-04-29T05:30:00+00:00",
    }
    assert key in _STATE


def test_v2_on_10m_rising_edge_long_entry():
    strat = TradeStratV2(params=TradeStratV2Params())
    bars = _bars([39000, 39100, 39200, 39300])
    indicators = _ind(period=14, plus=25.0, minus=10.0)
    ev = BarEvent(
        symbol="MXF",
        resolution="10m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = strat.on_bar(ev)

    assert sig is not None
    assert sig.side == "LONG"
    assert sig.price == 39300
    state = mod._state_for(TradeStratV2.name, "MXF")
    assert state.position is not None
    assert state.position.entry_price == 39300


def test_v2_on_10m_no_repeat_without_reset():
    strat = TradeStratV2(params=TradeStratV2Params())
    bars = _bars([39000, 39100, 39200, 39300])
    indicators = _ind(period=14, plus=25.0, minus=10.0)
    ev = BarEvent(
        symbol="MXF",
        resolution="10m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    first = strat.on_bar(ev)
    assert first is not None and first.side == "LONG"

    # Same conditions on next bar: no rising edge, no fresh entry.
    second = TradeStratV2(params=TradeStratV2Params()).on_bar(ev)
    assert second is None


def test_v2_daily_confidence_count():
    strat = TradeStratV2(params=TradeStratV2Params())
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
    state = mod._state_for(TradeStratV2.name, "MXF")
    assert state.daily_confidence_long == 3
    assert state.daily_confidence_short == 0


def test_v2_2m_exit_assist_di_flip():
    strat = TradeStratV2(params=TradeStratV2Params())
    # Pre-existing LONG position; -DI flips above exit threshold (23) on 2m.
    st = mod._state_for(TradeStratV2.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=39000.0,
        entry_ts=datetime(2026, 4, 29, 5, 0, tzinfo=UTC),
    )
    bars = _bars([39000, 39050, 39020, 38980])
    indicators = _ind(period=14, plus=10.0, minus=30.0)
    ev = BarEvent(
        symbol="MXF",
        resolution="2m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = strat.on_bar(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert "DI_FLIP" in sig.reason
    state = mod._state_for(TradeStratV2.name, "MXF")
    assert state.position is None


def test_v2_cooldown_blocks_reentry():
    # Set cooldown via close, then verify it counts down on each 10m bar
    # without permitting a re-entry while still > 0.
    strat = TradeStratV2(params=TradeStratV2Params())
    st = mod._state_for(TradeStratV2.name, "MXF")
    st.cooldown_left = 5

    bars = _bars([39000, 39100, 39200, 39300])
    indicators = _ind(period=14, plus=25.0, minus=10.0)
    ev = BarEvent(
        symbol="MXF",
        resolution="10m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    # First 4 bars after the close: cooldown_left should remain > 0, no signal.
    for expected_after in (4, 3, 2, 1):
        sig = strat.on_bar(ev)
        assert sig is None
        assert st.cooldown_left == expected_after
        assert st.position is None
