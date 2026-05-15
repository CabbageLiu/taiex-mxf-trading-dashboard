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


def _bars(closes: list[float], freq: str = "5min") -> pd.DataFrame:
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
    macd_series: list[float] | None = None,
    freq: str = "5min",
) -> dict[str, pd.DataFrame]:
    """Build indicator frames.

    `macd_series` lets a test express the rising-edge pattern explicitly:
    e.g. [-1, 1, 2] satisfies macd[-3]<=0, macd[-2]>0, macd[-1]>macd[-2].
    Default keeps the legacy constant-1 shape (no rising edge).
    """
    if macd_series is None:
        macd_series = [-1.0, 1.0, 2.0] + [2.0] * (period - 2)
    n = len(macd_series)
    idx = pd.date_range("2026-04-29", periods=n, freq=freq, tz="UTC")
    macd_arr = np.asarray(macd_series, dtype=float)
    return {
        "kd": pd.DataFrame(
            {"k": np.full(n, k), "d": np.full(n, d)}, index=idx
        ),
        "macd": pd.DataFrame(
            {
                "macd": macd_arr,
                "signal": np.zeros(n),
                "hist": macd_arr,
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


def _entry_event(
    plus: float = 25.0,
    minus: float = 10.0,
    k: float = 50.0,
    d: float = 50.0,
    macd_series: list[float] | None = None,
) -> BarEvent:
    bars = _bars([39000, 39100, 39200, 39300])
    indicators = _ind(
        period=14, plus=plus, minus=minus, k=k, d=d, macd_series=macd_series
    )
    return BarEvent(
        symbol="MXF",
        resolution="5m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )


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


def test_v2_macd_rising_edge_positive():
    """Entry fires when MACD just turned positive (rising-edge)."""
    strat = TradeStratV2(params=TradeStratV2Params())
    ev = _entry_event(macd_series=[-1.0, 1.0, 2.0])

    sig = strat.on_bar(ev)

    assert sig is not None
    assert sig.side == "LONG"
    assert sig.price == 39300


@pytest.mark.parametrize(
    "macd_series",
    [
        # macd[-3] > 0 — already positive too long.
        [1.0, 2.0, 3.0],
        # macd[-2] <= 0 — never crossed.
        [-2.0, -1.0, 1.0],
        # macd[-1] <= macd[-2] — not still rising.
        [-1.0, 2.0, 1.5],
    ],
)
def test_v2_macd_not_rising_no_entry(macd_series):
    strat = TradeStratV2(params=TradeStratV2Params())
    ev = _entry_event(macd_series=macd_series)

    sig = strat.on_bar(ev)

    assert sig is None
    state = mod._state_for(TradeStratV2.name, "MXF")
    assert state.position is None


def test_v2_5m_does_not_fire_tp_sl():
    """V2 spec: TP/SL is 1m-only. A +75 pt move on the 5m path does NOT close.

    The 5m bar after entry should only refresh the rising-edge latches and
    return None — exits live on `_check_tp_sl_minute` (1m) and `_exit_assist`
    (3m). This guards against regressing back to v1's behaviour where the
    entry timeframe also evaluated TP/SL.
    """
    strat = TradeStratV2(params=TradeStratV2Params())
    ev = _entry_event(macd_series=[-1.0, 1.0, 2.0])
    first = strat.on_bar(ev)
    assert first is not None and first.side == "LONG"
    entry_price = first.price

    # Same 5m timeframe, +75 pts above entry — must NOT emit a TP exit.
    later_idx = pd.date_range("2026-04-29 00:20", periods=4, freq="5min", tz="UTC")
    closes = [entry_price + 75.0] * 4
    arr = np.asarray(closes, dtype=float)
    bars2 = pd.DataFrame(
        {
            "open": arr,
            "high": arr,
            "low": arr,
            "close": arr,
            "tick_count": np.full(4, 1, dtype=int),
        },
        index=later_idx,
    )
    indicators2 = _ind(period=14, plus=25.0, minus=10.0, macd_series=[1.0, 2.0, 2.5])
    ev2 = BarEvent(
        symbol="MXF",
        resolution="5m",
        bucket=bars2.index[-1].to_pydatetime(),
        bars=bars2,
        indicators=indicators2,
    )

    sig2 = TradeStratV2(params=TradeStratV2Params()).on_bar(ev2)

    assert sig2 is None
    state = mod._state_for(TradeStratV2.name, "MXF")
    assert state.position is not None  # position still open

    # And the same +75 pt move, expressed as a 1m bar, DOES trigger TP.
    bars1m = _bars([entry_price + 75.0] * 3, freq="1min")
    ev1m = BarEvent(
        symbol="MXF",
        resolution="1m",
        bucket=bars1m.index[-1].to_pydatetime(),
        bars=bars1m,
        indicators={},
    )
    sig3 = TradeStratV2(params=TradeStratV2Params()).on_bar(ev1m)

    assert sig3 is not None
    assert sig3.side == "EXIT"
    assert sig3.payload["exit_reason"] == "TP"
    assert sig3.payload["pnl_points"] == 75.0


def test_v2_1m_tp_sl_eval_separate_from_entry():
    """1m bar with NO indicators still triggers TP via pure pnl math."""
    # Per v2's fixed-shape contract, _PositionState.entry_ind always carries
    # all 8 keys (None for missing). Build the seed snapshot accordingly so
    # the fallback path round-trips through the payload unchanged.
    seed_entry_ind = {
        "k": 55.0,
        "d": 50.0,
        "macd": 2.0,
        "signal": None,
        "hist": None,
        "plus_di": 25.0,
        "minus_di": None,
        "adx": None,
    }
    st = mod._state_for(TradeStratV2.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=39000.0,
        entry_ts=datetime(2026, 4, 29, 5, 0, tzinfo=UTC),
        entry_ind=seed_entry_ind,
    )

    # Pure 1m bars, no indicator dict — close 75 pts above entry.
    bars1m = _bars([39075, 39076, 39077, 39078], freq="1min")
    ev = BarEvent(
        symbol="MXF",
        resolution="1m",
        bucket=bars1m.index[-1].to_pydatetime(),
        bars=bars1m,
        indicators={},  # NO entry logic should fire here.
    )

    sig = TradeStratV2(params=TradeStratV2Params()).on_bar(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"
    assert sig.payload["pnl_points"] == 78.0
    # exit_ind falls back to the entry-time 5m snapshot stored on
    # _PositionState — must contain all 8 keys with the seeded values
    # (None for missing). Variable-shape would break the frontend type.
    snap = sig.payload["exit_ind"]
    assert set(snap.keys()) == {
        "k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx"
    }
    assert snap == seed_entry_ind
    state = mod._state_for(TradeStratV2.name, "MXF")
    assert state.position is None


def test_v2_di_flip_uses_gte_not_gt():
    """v2 uses `-DI >= 23`. -DI exactly 23.0 must trigger exit (v1 would skip)."""
    strat = TradeStratV2(params=TradeStratV2Params())
    st = mod._state_for(TradeStratV2.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=39000.0,
        entry_ts=datetime(2026, 4, 29, 5, 0, tzinfo=UTC),
    )

    bars = _bars([39000, 39050, 39020, 38980], freq="3min")
    indicators = _ind(period=14, plus=10.0, minus=23.0, freq="3min")
    ev = BarEvent(
        symbol="MXF",
        resolution="3m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = strat.on_bar(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert "DI_FLIP" in sig.reason


def test_v2_open_position_payload_carries_entry_ind():
    strat = TradeStratV2(params=TradeStratV2Params())
    ev = _entry_event(
        plus=25.0, minus=10.0, k=55.0, d=52.0, macd_series=[-1.0, 1.0, 2.0]
    )

    sig = strat.on_bar(ev)

    assert sig is not None
    snap = sig.payload["entry_ind"]
    for key in ("k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx"):
        assert key in snap
    assert snap["k"] == 55.0
    assert snap["d"] == 52.0
    assert snap["plus_di"] == 25.0
    assert snap["minus_di"] == 10.0
    # Legacy `entry` key kept for fixture back-compat.
    assert sig.payload["entry"]["k"] == 55.0
    assert sig.payload["entry"]["di"] == 25.0


def test_v2_close_position_payload_carries_exit_ind():
    """exit_ind populated when the 3m DI-flip path closes."""
    strat = TradeStratV2(params=TradeStratV2Params())
    st = mod._state_for(TradeStratV2.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=39000.0,
        entry_ts=datetime(2026, 4, 29, 5, 0, tzinfo=UTC),
    )

    bars = _bars([39000, 39050, 39020, 38980], freq="3min")
    indicators = _ind(period=14, plus=10.0, minus=30.0, freq="3min")
    ev = BarEvent(
        symbol="MXF",
        resolution="3m",
        bucket=bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=indicators,
    )

    sig = strat.on_bar(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    snap = sig.payload["exit_ind"]
    # Fixed 8-key contract — every key is present, missing values are None.
    assert set(snap.keys()) == {
        "k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx"
    }
    assert snap["plus_di"] == 10.0
    assert snap["minus_di"] == 30.0
    assert snap["macd"] is not None
    assert snap["k"] is not None


def test_v2_short_macd_requires_falling_edge_gate():
    """SHORT entry mirrors LONG: MACD must just have crossed *below* 0.

    A stale negative MACD (e.g. macd[-3]=-1, macd[-2]=-2, macd[-1]=-3)
    means MACD has been negative all along — no fresh cross down — so the
    rising-edge-mirror (`_macd_rising_edge(-macd)`) returns False and the
    SHORT entry must NOT fire even though every other condition holds.
    """
    params = TradeStratV2Params(enable_short=True)
    strat = TradeStratV2(params=params)
    # KD < 80, +DI < -DI, -DI > 21 → DMI + KD short gate satisfied.
    # MACD strictly negative, monotonically decreasing → no rising-edge
    # of the negated series, i.e. no fresh downside cross.
    ev = _entry_event(
        plus=10.0, minus=25.0, k=40.0, d=40.0,
        macd_series=[-1.0, -2.0, -3.0],
    )

    sig = strat.on_bar(ev)

    assert sig is None
    state = mod._state_for(TradeStratV2.name, "MXF")
    assert state.position is None

    # Sanity: a true falling-edge pattern (was non-negative 3 bars ago,
    # turned negative 2 bars ago, kept falling on the latest bar) DOES
    # fire — the gate is symmetric to LONG.
    ev_falling = _entry_event(
        plus=10.0, minus=25.0, k=40.0, d=40.0,
        macd_series=[1.0, -1.0, -2.0],
    )
    sig2 = TradeStratV2(params=params).on_bar(ev_falling)
    assert sig2 is not None
    assert sig2.side == "SHORT"


def test_v2_on_5m_rising_edge_long_entry():
    strat = TradeStratV2(params=TradeStratV2Params())
    ev = _entry_event(macd_series=[-1.0, 1.0, 2.0])

    sig = strat.on_bar(ev)

    assert sig is not None
    assert sig.side == "LONG"
    assert sig.price == 39300
    state = mod._state_for(TradeStratV2.name, "MXF")
    assert state.position is not None
    assert state.position.entry_price == 39300


def test_v2_on_5m_no_repeat_without_reset():
    strat = TradeStratV2(params=TradeStratV2Params())
    ev = _entry_event(macd_series=[-1.0, 1.0, 2.0])

    first = strat.on_bar(ev)
    assert first is not None and first.side == "LONG"

    # Same conditions on next bar: no rising edge, no fresh entry.
    second = TradeStratV2(params=TradeStratV2Params()).on_bar(ev)
    assert second is None


def test_v2_daily_confidence_count():
    strat = TradeStratV2(params=TradeStratV2Params())
    # K, D = 85 → above the short_ceiling (80), so the short side gets 0 on KD.
    # plus_di > 21 AND > -DI, minus_di < 21, macd > 0 → long 3/3, short 0/3.
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


def test_v2_3m_exit_assist_di_flip():
    strat = TradeStratV2(params=TradeStratV2Params())
    # Pre-existing LONG position; -DI flips above exit threshold (>=23) on 3m.
    st = mod._state_for(TradeStratV2.name, "MXF")
    st.position = _PositionState(
        side="LONG",
        entry_price=39000.0,
        entry_ts=datetime(2026, 4, 29, 5, 0, tzinfo=UTC),
    )
    bars = _bars([39000, 39050, 39020, 38980], freq="3min")
    indicators = _ind(period=14, plus=10.0, minus=30.0, freq="3min")
    ev = BarEvent(
        symbol="MXF",
        resolution="3m",
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
    # Set cooldown via close, then verify it counts down on each 5m bar
    # without permitting a re-entry while still > 0.
    strat = TradeStratV2(params=TradeStratV2Params())
    st = mod._state_for(TradeStratV2.name, "MXF")
    st.cooldown_left = 5

    ev = _entry_event(macd_series=[-1.0, 1.0, 2.0])

    # First 4 bars after the close: cooldown_left should remain > 0, no signal.
    for expected_after in (4, 3, 2, 1):
        sig = strat.on_bar(ev)
        assert sig is None
        assert st.cooldown_left == expected_after
        assert st.position is None
