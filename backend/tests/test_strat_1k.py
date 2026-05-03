from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BarEvent
from app.strategies.examples import strat_1k as mod
from app.strategies.examples.strat_1k import (
    _STATE,
    TradeStrat1K,
    TradeStrat1KParams,
    _PositionState,
    _StratState,
)

RES = "1m"
FREQ = "1min"
SYM = "MXF"


@pytest.fixture(autouse=True)
def reset_state():
    _STATE.clear()
    yield
    _STATE.clear()


def _bars(n: int, *, last_close: float, slope: float = 0.5) -> pd.DataFrame:
    idx = pd.date_range(
        end=datetime(2026, 5, 1, tzinfo=UTC), periods=n, freq=FREQ
    )
    closes = np.linspace(last_close - slope * (n - 1), last_close, n)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "tick_count": np.full(n, 1, dtype=int),
        },
        index=idx,
    )


def _inds(
    bars: pd.DataFrame,
    *,
    ma_prev: float = 100.0,
    ma_curr: float = 100.5,
    k_prev: float = 75.0,
    d_prev: float = 50.0,
    k_curr: float = 78.0,
    d_curr: float = 60.0,
    hist_prev: float = -1.0,
    hist_curr: float = 1.0,
    plus_prev: float = 25.0,
    plus_curr: float = 28.0,
    minus_prev: float = 18.0,
    minus_curr: float = 12.0,
    macd_curr: float = 1.0,
    signal_curr: float = 0.0,
    adx_curr: float = 25.0,
) -> dict[str, pd.DataFrame]:
    n = len(bars)
    idx = bars.index

    ma = np.full(n, ma_prev, dtype=float)
    ma[-1] = ma_curr
    if n >= 2:
        ma[-2] = ma_prev

    k = np.full(n, k_prev, dtype=float)
    d = np.full(n, d_prev, dtype=float)
    if n >= 2:
        k[-2] = k_prev
        d[-2] = d_prev
    k[-1] = k_curr
    d[-1] = d_curr

    hist = np.full(n, hist_prev, dtype=float)
    if n >= 2:
        hist[-2] = hist_prev
    hist[-1] = hist_curr

    plus = np.full(n, plus_prev, dtype=float)
    minus = np.full(n, minus_prev, dtype=float)
    if n >= 2:
        plus[-2] = plus_prev
        minus[-2] = minus_prev
    plus[-1] = plus_curr
    minus[-1] = minus_curr

    macd_arr = np.full(n, macd_curr, dtype=float)
    signal_arr = np.full(n, signal_curr, dtype=float)

    return {
        "ma120": pd.DataFrame({"ma": ma}, index=idx),
        "kd": pd.DataFrame({"k": k, "d": d}, index=idx),
        "macd": pd.DataFrame(
            {"macd": macd_arr, "signal": signal_arr, "hist": hist}, index=idx
        ),
        "dmi": pd.DataFrame(
            {
                "plus_di": plus,
                "minus_di": minus,
                "adx": np.full(n, adx_curr, dtype=float),
            },
            index=idx,
        ),
    }


def _event(
    bars: pd.DataFrame, inds: dict[str, pd.DataFrame], bucket=None
) -> BarEvent:
    return BarEvent(
        symbol=SYM,
        resolution=RES,
        bucket=bucket or bars.index[-1].to_pydatetime(),
        bars=bars,
        indicators=inds,
    )


# ─── 1. entry happy path ─────────────────────────────────────────────────


def test_entry_happy_path():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))

    assert sig is not None
    assert sig.side == "LONG"
    assert sig.price == 200.0
    snap = sig.payload["entry_ind"]
    for key in ("k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx"):
        assert key in snap
    assert sig.payload["tp_points"] == 50.0
    assert sig.payload["sl_points"] == 40.0
    assert sig.payload["trail_points"] == 50.0
    assert sig.payload["di_jump_points"] == 5.0


# ─── 2. MA fail ──────────────────────────────────────────────────────────


def test_no_entry_when_close_not_above_ma():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=100.0)
    inds = _inds(bars, ma_prev=100.0, ma_curr=100.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 3. MA flat ──────────────────────────────────────────────────────────


def test_no_entry_when_ma_flat():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, ma_prev=100.0, ma_curr=100.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 4. KD floor ─────────────────────────────────────────────────────────


def test_no_entry_when_first_k_at_floor():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=80.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_at_kd_boundary_below_floor():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=79.99)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


# ─── 5. MACD wrong sign ──────────────────────────────────────────────────


def test_no_entry_when_hist_prev_positive():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_prev=0.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_hist_curr_zero():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_curr=0.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 6. DMI not flipping ─────────────────────────────────────────────────


def test_no_entry_when_minus_di_flat():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, minus_prev=18.0, minus_curr=18.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 7. rising-edge ──────────────────────────────────────────────────────


def test_rising_edge_suppresses_when_already_ready():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    _STATE[(TradeStrat1K.name, SYM)] = _StratState(last_long_ready=True)
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None
    st = _STATE[(TradeStrat1K.name, SYM)]
    assert st.position is None
    assert st.last_long_ready is True


# ─── 8. TP exit ──────────────────────────────────────────────────────────


def test_tp_exit_at_threshold():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig1 = strat.on_bar(_event(bars, inds))
    assert sig1 is not None and sig1.side == "LONG"

    bars2 = _bars(5, last_close=200.0 + 50.0)
    bucket2 = bars2.index[-1].to_pydatetime() + pd.Timedelta(minutes=1)
    inds2 = _inds(bars2)
    sig2 = TradeStrat1K(params=TradeStrat1KParams()).on_bar(
        _event(bars2, inds2, bucket=bucket2)
    )
    assert sig2 is not None
    assert sig2.side == "EXIT"
    assert sig2.payload["exit_reason"] == "TP"
    assert sig2.payload["pnl_points"] == 50.0

    st = _STATE[(TradeStrat1K.name, SYM)]
    assert st.position is None
    assert st.cooldown_left == 5
    assert st.last_long_ready is False


# ─── 9. SL exit ──────────────────────────────────────────────────────────


def test_sl_exit_at_threshold():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    strat.on_bar(_event(bars, inds))

    bars2 = _bars(5, last_close=200.0 - 40.0)
    bucket2 = bars.index[-1].to_pydatetime() + pd.Timedelta(minutes=1)
    inds2 = _inds(bars2)
    sig2 = TradeStrat1K(params=TradeStrat1KParams()).on_bar(
        _event(bars2, inds2, bucket=bucket2)
    )
    assert sig2 is not None
    assert sig2.side == "EXIT"
    assert sig2.payload["exit_reason"] == "SL"
    assert sig2.payload["pnl_points"] == -40.0


# ─── 10. trailing stop ───────────────────────────────────────────────────


def test_trail_exit_after_peak():
    """Seed open at 100. tp=50, sl=40, trail=50.

      Bar A (close=130): pnl=+30 < tp=50 (no TP), |pnl|<sl=40 (no SL), pnl=30
        not ≤ peak(0)−50=−50 (no TRAIL). peak updates to 30.
      Bar B (close=85): pnl=−15. |pnl|<sl. peak(30)−50=−20, −15 > −20 → no fire.
      Bar C (close=80): pnl=−20. peak(30)−50=−20, −20 ≤ −20 → TRAIL fires.
    """
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars2 = _bars(5, last_close=130.0)
    inds2 = _inds(bars2)
    sig2 = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars2, inds2))
    assert sig2 is None
    assert st.position is not None
    assert st.position.peak_pnl == 30.0

    bars2b = _bars(5, last_close=85.0)
    inds2b = _inds(bars2b)
    sig2b = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars2b, inds2b))
    assert sig2b is None
    assert st.position is not None

    bars3 = _bars(5, last_close=80.0)
    inds3 = _inds(bars3)
    sig3 = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars3, inds3))
    assert sig3 is not None
    assert sig3.side == "EXIT"
    assert sig3.payload["exit_reason"] == "TRAIL"
    assert sig3.payload["pnl_points"] == -20.0


# ─── 11. cooldown ────────────────────────────────────────────────────────


def test_cooldown_blocks_reentry_for_5_bars():
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=50.0)  # pnl=−50 ≤ −sl=40 → SL.
    inds = _inds(bars)
    sig_exit = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars, inds))
    assert sig_exit is not None and sig_exit.side == "EXIT"
    assert st.cooldown_left == 5

    bars_e = _bars(5, last_close=200.0)
    inds_e = _inds(bars_e)
    for expected_after in (4, 3, 2, 1, 0):
        sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars_e, inds_e))
        assert sig is None
        assert st.cooldown_left == expected_after
        assert st.position is None

    bars_low = _bars(5, last_close=200.0)
    inds_low = _inds(bars_low, hist_prev=0.5)
    sig_low = TradeStrat1K(params=TradeStrat1KParams()).on_bar(
        _event(bars_low, inds_low)
    )
    assert sig_low is None
    assert st.last_long_ready is False

    sig_fire = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars_e, inds_e))
    assert sig_fire is not None
    assert sig_fire.side == "LONG"


# ─── 12. DI_JUMP exit (strat_1k only) ────────────────────────────────────


def test_di_jump_fires_when_minus_di_jumps_above_threshold():
    """-DI 15 → 21 (jump=6 > 5) while position open → EXIT DI_JUMP_1M."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    # Close above entry but below TP (pnl=10, tp=50). SL/TRAIL also clear.
    bars = _bars(5, last_close=110.0)
    # -DI: prev=15, curr=21 → jump=6 > 5 → DI_JUMP fires.
    inds = _inds(bars, minus_prev=15.0, minus_curr=21.0, plus_curr=28.0)
    sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars, inds))

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "DI_JUMP_1M"
    assert sig.payload["pnl_points"] == 10.0
    assert st.position is None
    assert st.cooldown_left == 5


def test_di_jump_no_fire_at_exact_threshold():
    """-DI jump = 5.0 exactly → no exit (strict `>`)."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=110.0)
    inds = _inds(bars, minus_prev=15.0, minus_curr=20.0)  # jump=5.0 exactly
    sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars, inds))

    assert sig is None
    assert st.position is not None


def test_di_jump_no_fire_below_threshold():
    """-DI jump = 4.9 → no exit."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=110.0)
    inds = _inds(bars, minus_prev=15.0, minus_curr=19.9)  # jump=4.9
    sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars, inds))

    assert sig is None
    assert st.position is not None
