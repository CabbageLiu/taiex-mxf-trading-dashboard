from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BarEvent
from app.strategies.examples import strat_30k as mod
from app.strategies.examples.strat_30k import (
    _STATE,
    TradeStrat30K,
    TradeStrat30KParams,
    _PositionState,
    _StratState,
)

RES = "30m"
FREQ = "30min"
SYM = "MXF"


@pytest.fixture(autouse=True)
def reset_state():
    _STATE.clear()
    yield
    _STATE.clear()


def _bars(n: int, *, last_close: float, slope: float = 0.5) -> pd.DataFrame:
    """Synthesize OHLC bars with monotonically rising close.

    `slope` (>0) makes the close series rising so we can place `last_close`
    above MA[-1]. open=high=low=close keeps things simple — strategies only
    read close.
    """
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
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    # MA[-1]=100.5 < close=200, MA rising; KD/MACD/DMI all aligned by default.
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))

    assert sig is not None
    assert sig.side == "LONG"
    assert sig.price == 200.0
    snap = sig.payload["entry_ind"]
    for key in ("k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx"):
        assert key in snap
    assert sig.payload["tp_points"] == 180.0
    assert sig.payload["sl_points"] == 70.0
    assert sig.payload["trail_points"] == 80.0


# ─── 2. MA fail — close ≤ MA ─────────────────────────────────────────────


def test_no_entry_when_close_not_above_ma():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=100.0)  # close == MA[-1]=100.5? No → 100<100.5
    inds = _inds(bars, ma_prev=100.0, ma_curr=100.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None
    assert _STATE[(TradeStrat30K.name, SYM)].position is None


# ─── 3. MA flat ──────────────────────────────────────────────────────────


def test_no_entry_when_ma_flat():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, ma_prev=100.0, ma_curr=100.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 4. KD floor ─────────────────────────────────────────────────────────


def test_no_entry_when_first_k_at_floor():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=80.0)  # k_prev < 80 must hold; 80 fails.
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_at_kd_boundary_below_floor():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=79.99)  # boundary just below floor → entry.
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


# ─── 5. MACD wrong sign ──────────────────────────────────────────────────


def test_no_entry_when_hist_prev_positive():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_prev=0.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_hist_curr_zero():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_curr=0.0)  # strict `> 0` required.
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 6. DMI not flipping ─────────────────────────────────────────────────


def test_no_entry_when_minus_di_flat():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, minus_prev=18.0, minus_curr=18.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 7. rising-edge — already latched true → no fire ─────────────────────


def test_rising_edge_suppresses_when_already_ready():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    # Pre-seed state with last_long_ready=True so entry_now=True is not a
    # rising edge (true→true) — must NOT fire.
    _STATE[(TradeStrat30K.name, SYM)] = _StratState(last_long_ready=True)
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None
    # State remains: position None, latch still true.
    st = _STATE[(TradeStrat30K.name, SYM)]
    assert st.position is None
    assert st.last_long_ready is True


# ─── 8. TP exit ──────────────────────────────────────────────────────────


def test_tp_exit_at_threshold():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig1 = strat.on_bar(_event(bars, inds))
    assert sig1 is not None and sig1.side == "LONG"

    # Bar 2: close = entry + TP exactly → TP fires.
    bars2 = _bars(5, last_close=200.0 + 180.0)
    bucket2 = bars2.index[-1].to_pydatetime() + pd.Timedelta(minutes=30)
    inds2 = _inds(bars2)
    sig2 = TradeStrat30K(params=TradeStrat30KParams()).on_bar(
        _event(bars2, inds2, bucket=bucket2)
    )
    assert sig2 is not None
    assert sig2.side == "EXIT"
    assert sig2.payload["exit_reason"] == "TP"
    assert sig2.payload["pnl_points"] == 180.0

    st = _STATE[(TradeStrat30K.name, SYM)]
    assert st.position is None
    assert st.cooldown_left == 5
    assert st.last_long_ready is False


# ─── 9. SL exit ──────────────────────────────────────────────────────────


def test_sl_exit_at_threshold():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    strat.on_bar(_event(bars, inds))

    bars2 = _bars(5, last_close=200.0 - 70.0)
    bucket2 = bars.index[-1].to_pydatetime() + pd.Timedelta(minutes=30)
    inds2 = _inds(bars2)
    sig2 = TradeStrat30K(params=TradeStrat30KParams()).on_bar(
        _event(bars2, inds2, bucket=bucket2)
    )
    assert sig2 is not None
    assert sig2.side == "EXIT"
    assert sig2.payload["exit_reason"] == "SL"
    assert sig2.payload["pnl_points"] == -70.0


# ─── 10. trailing stop ───────────────────────────────────────────────────


def test_trail_exit_after_peak():
    """Seed an open position at 100; verify TRAIL math.

    With tp=180, sl=70, trail=80:
      Bar 2 (close=150): pnl=+50. <TP, |pnl|<SL, pnl=50 not ≤ peak(0)−80=−80
        → no exit; peak updates to 50.
      Bar 3 (close=80): pnl=−20. >−SL, peak(50)−80=−30, −20 > −30 → no exit.
      Bar 4 (close=70): pnl=−30. peak(50)−80=−30, −30 ≤ −30 → TRAIL fires.
    """
    st = mod._state_for(TradeStrat30K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars2 = _bars(5, last_close=150.0)
    inds2 = _inds(bars2)
    sig2 = TradeStrat30K(params=TradeStrat30KParams()).on_bar(_event(bars2, inds2))
    assert sig2 is None
    assert st.position is not None
    assert st.position.peak_pnl == 50.0

    bars3 = _bars(5, last_close=80.0)
    inds3 = _inds(bars3)
    sig3 = TradeStrat30K(params=TradeStrat30KParams()).on_bar(_event(bars3, inds3))
    assert sig3 is None
    assert st.position is not None
    assert st.position.peak_pnl == 50.0  # unchanged (max(50, −20) = 50)

    bars4 = _bars(5, last_close=70.0)
    inds4 = _inds(bars4)
    sig4 = TradeStrat30K(params=TradeStrat30KParams()).on_bar(_event(bars4, inds4))
    assert sig4 is not None
    assert sig4.side == "EXIT"
    assert sig4.payload["exit_reason"] == "TRAIL"
    assert sig4.payload["pnl_points"] == -30.0


# ─── 11. cooldown ────────────────────────────────────────────────────────


def test_cooldown_blocks_reentry_for_5_bars():
    # Manually open then close to set cooldown.
    st = mod._state_for(TradeStrat30K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    # Drive an SL exit.
    bars = _bars(5, last_close=30.0)
    inds = _inds(bars)
    sig_exit = TradeStrat30K(params=TradeStrat30KParams()).on_bar(_event(bars, inds))
    assert sig_exit is not None and sig_exit.side == "EXIT"
    assert st.cooldown_left == 5

    # Next 5 entry-condition bars should be suppressed by cooldown.
    bars_e = _bars(5, last_close=200.0)
    inds_e = _inds(bars_e)
    for expected_after in (4, 3, 2, 1, 0):
        sig = TradeStrat30K(params=TradeStrat30KParams()).on_bar(_event(bars_e, inds_e))
        assert sig is None
        assert st.cooldown_left == expected_after
        assert st.position is None

    # Bar after — cooldown=0, but last_long_ready latch may have been set on
    # the previous bar. The close above already reset last_long_ready=False.
    # Each cooldown bar above set last_long_ready to long_now (True since
    # gates align). So the 6th bar evaluates true→true (no rising edge).
    # To re-fire, we need a low→true edge: feed one false-gate bar first.
    bars_low = _bars(5, last_close=200.0)
    inds_low = _inds(bars_low, hist_prev=0.5)  # MACD wrong sign breaks gate.
    sig_low = TradeStrat30K(params=TradeStrat30KParams()).on_bar(
        _event(bars_low, inds_low)
    )
    assert sig_low is None
    assert st.last_long_ready is False

    # Now a clean rising-edge bar fires entry.
    sig_fire = TradeStrat30K(params=TradeStrat30KParams()).on_bar(_event(bars_e, inds_e))
    assert sig_fire is not None
    assert sig_fire.side == "LONG"


def test_cooldown_5th_bar_still_blocked_when_gates_just_fire():
    """Regression: bars 1-4 post-exit have false gates; bar 5 has true gates
    with rising-edge intent. Spec says 5 bars must elapse before re-entry —
    bar 5 must remain blocked. Earlier off-by-one leaked entry on bar 5."""
    st = mod._state_for(TradeStrat30K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    # SL exit to set cooldown=5.
    bars_exit = _bars(5, last_close=30.0)
    sig_exit = TradeStrat30K(params=TradeStrat30KParams()).on_bar(
        _event(bars_exit, _inds(bars_exit))
    )
    assert sig_exit is not None and sig_exit.side == "EXIT"
    assert st.cooldown_left == 5

    # 4 bars with FALSE gates (MACD wrong sign breaks gate).
    bars_false = _bars(5, last_close=200.0)
    inds_false = _inds(bars_false, hist_prev=0.5)
    for expected_after in (4, 3, 2, 1):
        sig = TradeStrat30K(params=TradeStrat30KParams()).on_bar(
            _event(bars_false, inds_false)
        )
        assert sig is None
        assert st.cooldown_left == expected_after
        assert st.last_long_ready is False  # gates false ⇒ latch false

    # 5th bar — gates flip true with a clean rising edge (latch is False).
    # Cooldown must still block this; the 5 elapsed bars include this one.
    bars_true = _bars(5, last_close=200.0)
    inds_true = _inds(bars_true)
    sig5 = TradeStrat30K(params=TradeStrat30KParams()).on_bar(
        _event(bars_true, inds_true)
    )
    assert sig5 is None, "5th post-exit bar must remain in cooldown"
    assert st.cooldown_left == 0
    # Latch was set True on this bar (gates aligned).
    assert st.last_long_ready is True

    # 6th bar — cooldown=0; latch already True so no rising edge.
    sig6 = TradeStrat30K(params=TradeStrat30KParams()).on_bar(
        _event(bars_true, inds_true)
    )
    assert sig6 is None
    # Reset latch with a false-gate bar, then a true-gate bar must finally fire.
    TradeStrat30K(params=TradeStrat30KParams()).on_bar(
        _event(bars_false, inds_false)
    )
    sig_ok = TradeStrat30K(params=TradeStrat30KParams()).on_bar(
        _event(bars_true, inds_true)
    )
    assert sig_ok is not None and sig_ok.side == "LONG"
