from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BarEvent, TickEvent
from app.strategies.examples import strat_1k_ai as mod
from app.strategies.examples.strat_1k_ai import (
    _STATE,
    TradeStrat1KAI,
    TradeStrat1KAIParams,
    _PositionState,
    _StratState,
)

RES = "1m"
FREQ = "1min"
SYM = "MXF"

DEFAULT_BAR_END = datetime(2026, 5, 1, 1, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def reset_state():
    _STATE.clear()
    yield
    _STATE.clear()


def _bars(
    n: int,
    *,
    last_close: float,
    slope: float = 0.5,
    end: datetime | None = None,
) -> pd.DataFrame:
    idx = pd.date_range(end=end or DEFAULT_BAR_END, periods=n, freq=FREQ)
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
    # LONG defaults satisfy every entry gate.
    k_prev: float = 60.0,
    d_prev: float = 50.0,
    k_curr: float = 78.0,
    d_curr: float = 60.0,
    hist_curr: float = 1.0,
    plus_prev: float = 25.0,
    plus_curr: float = 28.0,
    minus_prev: float = 18.0,
    minus_curr: float = 12.0,
    macd_curr: float = 1.0,
    signal_curr: float = 0.0,
    adx_curr: float = 25.0,
    atr_curr: float = 10.0,
) -> dict[str, pd.DataFrame]:
    n = len(bars)
    idx = bars.index

    k = np.full(n, k_prev, dtype=float)
    d = np.full(n, d_prev, dtype=float)
    if n >= 2:
        k[-2] = k_prev
        d[-2] = d_prev
    k[-1] = k_curr
    d[-1] = d_curr

    hist = np.full(n, hist_curr, dtype=float)
    if n >= 2:
        hist[-2] = hist_curr
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
    atr_arr = np.full(n, atr_curr, dtype=float)

    return {
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
        "atr": pd.DataFrame({"atr": atr_arr}, index=idx),
    }


def _short_inds(bars: pd.DataFrame, **overrides) -> dict[str, pd.DataFrame]:
    """Mirror of LONG defaults: gates flipped to satisfy SHORT entry."""
    defaults: dict[str, float] = {
        "k_prev": 40.0,        # > kd_short_ceiling=30
        "d_prev": 50.0,
        "k_curr": 22.0,
        "d_curr": 40.0,
        "hist_curr": -1.0,
        "plus_prev": 28.0,
        "plus_curr": 25.0,
        "minus_prev": 18.0,
        "minus_curr": 24.0,
        "macd_curr": -1.0,
        "signal_curr": 0.0,
        "adx_curr": 25.0,
        "atr_curr": 10.0,
    }
    defaults.update(overrides)
    return _inds(bars, **defaults)


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


def _tick_event(
    bars: pd.DataFrame,
    inds: dict[str, pd.DataFrame],
    *,
    ts: datetime,
    price: float,
):
    return TickEvent(
        symbol=SYM,
        resolution=RES,
        ts=ts,
        price=price,
        bars=bars,
        indicators=inds,
    )


def _open_position(*, side: str = "LONG", entry_price: float = 100.0,
                    entry_ts: datetime = DEFAULT_BAR_END,
                    sl_distance: float = 30.0) -> _StratState:
    st = mod._state_for(TradeStrat1KAI.name, SYM)
    st.position = _PositionState(
        side=side, entry_price=entry_price, entry_ts=entry_ts,
        peak_pnl=0.0, sl_distance=sl_distance,
    )
    return st


# ─── 1. entry parity with strat_1k (LONG) ────────────────────────────────


def test_entry_long_happy_path_parity():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))

    assert sig is not None
    assert sig.side == "LONG"
    assert sig.price == 200.0
    payload = sig.payload
    assert payload["tp_points"] == 50.0
    assert payload["sl_points"] == pytest.approx(18.0)  # ATR 10 * 1.8 = 18 (== floor)
    assert payload["atr_at_entry"] == 10.0
    assert payload["be_trigger_points"] == 15.0
    assert payload["trail_arm_points"] == 25.0
    assert payload["trail_give_back_points"] == 18.0


def test_no_entry_when_first_k_at_floor():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=70.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_hist_zero():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_curr=0.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 2. SHORT mirror ─────────────────────────────────────────────────────


def test_short_entry_happy_path():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams(enable_short=True))
    bars = _bars(5, last_close=200.0)
    inds = _short_inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "SHORT"
    assert "-DI" in sig.reason


def test_short_disabled_blocks_entry():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams(enable_short=False))
    bars = _bars(5, last_close=200.0)
    inds = _short_inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_short_when_k_below_ceiling():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams(enable_short=True))
    bars = _bars(5, last_close=200.0)
    inds = _short_inds(bars, k_prev=29.99)  # below default ceiling 30
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 3. ATR clamp on SL distance ─────────────────────────────────────────


def test_sl_distance_at_floor():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, atr_curr=5.0)  # 5 * 1.8 = 9 → clamped to floor 18
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["sl_points"] == 18.0


def test_sl_distance_at_cap():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, atr_curr=100.0)  # 100 * 1.8 = 180 → cap 45
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["sl_points"] == 45.0


def test_sl_distance_inside_band():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, atr_curr=20.0)  # 20 * 1.8 = 36
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["sl_points"] == pytest.approx(36.0)


def test_use_atr_sl_false_falls_back_to_fixed():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams(use_atr_sl=False))
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, atr_curr=20.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["sl_points"] == 30.0  # hard_sl_points default


# ─── 4. Hard SL exit ─────────────────────────────────────────────────────


def test_hard_sl_fires_at_threshold():
    st = _open_position(entry_price=100.0, sl_distance=30.0)
    bars = _bars(5, last_close=70.0)  # pnl = -30, hits SL
    inds = _inds(bars)
    sig = TradeStrat1KAI(params=TradeStrat1KAIParams()).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["exit_reason"] == "SL"
    assert st.position is None


# ─── 5. Break-even exit ──────────────────────────────────────────────────


def test_break_even_arms_then_fires():
    p = TradeStrat1KAIParams()
    st = _open_position(entry_price=100.0, sl_distance=50.0)
    # First tick: price 115 → pnl=+15, arms BE (peak ≥ trigger=15).
    bars1 = _bars(5, last_close=115.0)
    inds1 = _inds(bars1)
    sig1 = TradeStrat1KAI(params=p).on_bar(_event(bars1, inds1))
    assert sig1 is None
    assert st.position is not None and st.position.breakeven_armed is True

    # Second tick: price back to 100 → pnl=0, BE fires.
    bars2 = _bars(5, last_close=100.0)
    inds2 = _inds(bars2)
    sig2 = TradeStrat1KAI(params=p).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    assert sig2 is not None
    assert sig2.payload["exit_reason"] == "BE"
    assert sig2.payload["pnl_points"] == 0.0


def test_break_even_does_not_fire_below_trigger():
    p = TradeStrat1KAIParams()
    st = _open_position(entry_price=100.0, sl_distance=50.0)
    # Peak reaches +14 (below trigger=15), price drops back to 100 → no BE.
    bars1 = _bars(5, last_close=114.0)
    inds1 = _inds(bars1)
    TradeStrat1KAI(params=p).on_bar(_event(bars1, inds1))
    assert st.position is not None and st.position.breakeven_armed is False

    bars2 = _bars(5, last_close=100.0)
    inds2 = _inds(bars2)
    sig2 = TradeStrat1KAI(params=p).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    assert sig2 is None
    assert st.position is not None  # still open


# ─── 6. Trail arm gate ───────────────────────────────────────────────────


def test_trail_dormant_below_arm():
    p = TradeStrat1KAIParams()
    st = _open_position(entry_price=100.0, sl_distance=100.0)
    # Push peak to +20 (< arm 25). Pullback to peak-30 should NOT fire trail.
    bars1 = _bars(5, last_close=120.0)
    inds1 = _inds(bars1)
    TradeStrat1KAI(params=p).on_bar(_event(bars1, inds1))
    assert st.position is not None and st.position.peak_pnl == 20.0

    # Pullback to -10. pnl = -10, peak_pnl=20. Trail trigger would be peak-give_back = 20-18 = 2.
    # pnl (-10) <= 2 → would fire IF trail were armed; but peak (20) < arm (25), so not armed.
    bars2 = _bars(5, last_close=90.0)
    inds2 = _inds(bars2)
    sig = TradeStrat1KAI(params=p).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    # BE armed at +20 → pnl 0 fires BE first, but we're at -10 → BE fires too.
    assert sig is not None
    assert sig.payload["exit_reason"] == "BE"  # BE wins over dormant TRAIL


def test_trail_fires_after_arm():
    p = TradeStrat1KAIParams()
    st = _open_position(entry_price=100.0, sl_distance=100.0)
    # Push peak to +30 (≥ arm 25). Pullback to +12 → trail at peak-give_back=30-18=12.
    bars1 = _bars(5, last_close=130.0)
    inds1 = _inds(bars1)
    TradeStrat1KAI(params=p).on_bar(_event(bars1, inds1))
    assert st.position is not None and st.position.peak_pnl == 30.0

    bars2 = _bars(5, last_close=112.0)
    inds2 = _inds(bars2)
    sig = TradeStrat1KAI(params=p).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    assert sig is not None
    assert sig.payload["exit_reason"] == "TRAIL"
    assert sig.payload["pnl_points"] == 12.0


# ─── 7. TP exit (unchanged from strat_1k) ────────────────────────────────


def test_tp_exit_uses_tod_bucket():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"

    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=10)
    ev = _tick_event(bars, inds, ts=tick_ts, price=sig_open.price + 50.0)
    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.payload["exit_reason"] == "TP"
    assert sig.payload["pnl_points"] == 50.0


# ─── 8. Time cutoff ──────────────────────────────────────────────────────


def test_time_cutoff_fires():
    p = TradeStrat1KAIParams()
    entry_ts = DEFAULT_BAR_END
    _open_position(entry_price=100.0, entry_ts=entry_ts, sl_distance=50.0)
    # 31 min later, price = 102 → pnl=2 < cutoff_min_pnl=5.
    later = entry_ts + timedelta(minutes=31)
    bars = _bars(5, last_close=102.0, end=later)
    inds = _inds(bars)
    ev = _tick_event(bars, inds, ts=later, price=102.0)
    sig = TradeStrat1KAI(params=p).on_tick(ev)
    assert sig is not None
    assert sig.payload["exit_reason"] == "TIME"


def test_time_cutoff_blocked_when_in_profit():
    p = TradeStrat1KAIParams()
    entry_ts = DEFAULT_BAR_END
    _open_position(entry_price=100.0, entry_ts=entry_ts, sl_distance=50.0)
    later = entry_ts + timedelta(minutes=31)
    bars = _bars(5, last_close=110.0, end=later)  # pnl=10 ≥ cutoff_min=5
    inds = _inds(bars)
    ev = _tick_event(bars, inds, ts=later, price=110.0)
    sig = TradeStrat1KAI(params=p).on_tick(ev)
    assert sig is None


# ─── 9. Crash regime override ────────────────────────────────────────────


def test_crash_override_fires_with_di_spread_and_tr_burst():
    p = TradeStrat1KAIParams()
    st = _open_position(entry_price=100.0, sl_distance=50.0)
    # Build bars where last bar has TR > 2 * atr (atr=10 → trigger TR > 20).
    bars = _bars(5, last_close=98.0)
    bars = bars.copy()
    bars.iloc[-1, bars.columns.get_loc("high")] = 105.0
    bars.iloc[-1, bars.columns.get_loc("low")] = 70.0   # TR = 35 > 20
    bars.iloc[-1, bars.columns.get_loc("close")] = 98.0
    inds = _inds(bars, plus_curr=15.0, minus_curr=25.0, atr_curr=10.0)
    # adverse = minus - plus = 10 > threshold 8.
    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["exit_reason"] == "CRASH"
    assert st.position is None


def test_crash_override_blocked_when_disabled():
    p = TradeStrat1KAIParams(crash_regime_exit=False)
    _open_position(entry_price=100.0, sl_distance=50.0)
    bars = _bars(5, last_close=98.0).copy()
    bars.iloc[-1, bars.columns.get_loc("high")] = 105.0
    bars.iloc[-1, bars.columns.get_loc("low")] = 70.0
    bars.iloc[-1, bars.columns.get_loc("close")] = 98.0
    inds = _inds(bars, plus_curr=15.0, minus_curr=25.0, atr_curr=10.0)
    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    # Either SL or BE fires instead; just verify it isn't CRASH.
    if sig is not None:
        assert sig.payload["exit_reason"] != "CRASH"


# ─── 10. Cooldown unchanged ──────────────────────────────────────────────


def test_cooldown_blocks_re_entry():
    p = TradeStrat1KAIParams()
    strat = TradeStrat1KAI(params=p)
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None
    # Force an exit via TP.
    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=5)
    ev_exit = _tick_event(bars, inds, ts=tick_ts, price=sig_open.price + 50.0)
    sig_exit = strat.on_tick(ev_exit)
    assert sig_exit is not None and sig_exit.payload["exit_reason"] == "TP"

    # Re-entry attempt 100s after exit must be blocked.
    early_ts = tick_ts + timedelta(seconds=100)
    ev_early = _tick_event(bars, inds, ts=early_ts, price=200.0)
    sig_early = strat.on_tick(ev_early)
    assert sig_early is None


# ─── 11. Module state isolation from strat_1k ─────────────────────────────


def test_state_dict_independent_from_strat_1k():
    from app.strategies.examples import strat_1k as legacy
    assert mod._STATE is not legacy._STATE


# ─── 12. EOW force-close ─────────────────────────────────────────────────


def _bars_at_taipei(taipei_hms: tuple[int, int, int]) -> pd.DataFrame:
    h, m, s = taipei_hms
    end_utc = datetime(2026, 5, 1, h, m, s, tzinfo=UTC) - timedelta(hours=8)
    return _bars(5, last_close=200.0, end=end_utc)


def _taipei_ts(hms: tuple[int, int, int]) -> datetime:
    h, m, s = hms
    return datetime(2026, 5, 1, h, m, s, tzinfo=UTC) - timedelta(hours=8)


def test_eow_force_close_outside_window():
    strat = TradeStrat1KAI(params=TradeStrat1KAIParams())
    bars = _bars_at_taipei((13, 0, 0))
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"

    # 14:30 Taipei → closed gap → EOW.
    tick_ts = _taipei_ts((14, 30, 0))
    ev = _tick_event(bars, inds, ts=tick_ts, price=sig_open.price + 40.0)
    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.payload["exit_reason"] == "EOW"


# ─── 13. SHORT-side exits ────────────────────────────────────────────────


def test_short_tp_exit():
    p = TradeStrat1KAIParams()
    st = mod._state_for(TradeStrat1KAI.name, SYM)
    st.position = _PositionState(
        side="SHORT", entry_price=200.0,
        entry_ts=DEFAULT_BAR_END,
        peak_pnl=0.0, sl_distance=50.0,
    )
    # SHORT pnl = entry - price. Price 150 → pnl=50, hits TP 50 in [08:45,10:31).
    bars = _bars(5, last_close=150.0)
    inds = _inds(bars)
    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["exit_reason"] == "TP"
    assert sig.payload["pnl_points"] == 50.0


def test_short_hard_sl_exit():
    p = TradeStrat1KAIParams()
    st = mod._state_for(TradeStrat1KAI.name, SYM)
    st.position = _PositionState(
        side="SHORT", entry_price=200.0,
        entry_ts=DEFAULT_BAR_END,
        peak_pnl=0.0, sl_distance=30.0,
    )
    # SHORT pnl = entry - price. Price 230 → pnl=-30 → SL.
    bars = _bars(5, last_close=230.0)
    inds = _inds(bars)
    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["exit_reason"] == "SL"
    assert st.position is None


def test_short_break_even_fires():
    p = TradeStrat1KAIParams()
    st = mod._state_for(TradeStrat1KAI.name, SYM)
    st.position = _PositionState(
        side="SHORT", entry_price=200.0,
        entry_ts=DEFAULT_BAR_END,
        peak_pnl=0.0, sl_distance=100.0,
    )
    # Push peak to +15 (price 185 → pnl=15).
    bars1 = _bars(5, last_close=185.0)
    inds1 = _inds(bars1)
    sig1 = TradeStrat1KAI(params=p).on_bar(_event(bars1, inds1))
    assert sig1 is None
    assert st.position.breakeven_armed is True

    # Price back to 200 → SHORT pnl=0 → BE fires.
    bars2 = _bars(5, last_close=200.0)
    inds2 = _inds(bars2)
    sig2 = TradeStrat1KAI(params=p).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    assert sig2 is not None
    assert sig2.payload["exit_reason"] == "BE"


def test_short_trail_fires_after_arm():
    p = TradeStrat1KAIParams()
    st = mod._state_for(TradeStrat1KAI.name, SYM)
    st.position = _PositionState(
        side="SHORT", entry_price=200.0,
        entry_ts=DEFAULT_BAR_END,
        peak_pnl=0.0, sl_distance=100.0,
    )
    # Peak to +30 (price 170).
    bars1 = _bars(5, last_close=170.0)
    inds1 = _inds(bars1)
    TradeStrat1KAI(params=p).on_bar(_event(bars1, inds1))
    assert st.position.peak_pnl == 30.0

    # Pullback to pnl +12 (price 188). trail trigger = peak - give_back = 30-18 = 12.
    bars2 = _bars(5, last_close=188.0)
    inds2 = _inds(bars2)
    sig = TradeStrat1KAI(params=p).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    assert sig is not None
    assert sig.payload["exit_reason"] == "TRAIL"
    assert sig.payload["pnl_points"] == 12.0


# ─── 14. NaN ATR → fallback to hard_sl_points ────────────────────────────


def test_nan_atr_falls_back_to_hard_sl():
    p = TradeStrat1KAIParams()
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    inds["atr"].iloc[-1, 0] = float("nan")
    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["sl_points"] == p.hard_sl_points


# ─── 15. Pydantic validator rejects pathological configs ─────────────────


def test_validator_rejects_inverted_atr_clamp():
    with pytest.raises(ValueError):
        TradeStrat1KAIParams(atr_sl_floor=50.0, atr_sl_cap=40.0)


def test_validator_rejects_zero_give_back():
    with pytest.raises(ValueError):
        TradeStrat1KAIParams(trail_give_back_points=0.0)


def test_validator_rejects_trail_arm_below_be_trigger():
    with pytest.raises(ValueError):
        TradeStrat1KAIParams(be_trigger_points=20.0, trail_arm_points=15.0)


def test_validator_rejects_negative_hard_sl():
    with pytest.raises(ValueError):
        TradeStrat1KAIParams(hard_sl_points=-1.0)


# ─── 16. Cooldown applies after non-TP exits too ─────────────────────────


def test_cooldown_after_sl_exit():
    p = TradeStrat1KAIParams()
    st = _open_position(entry_price=100.0, sl_distance=30.0)
    bars = _bars(5, last_close=70.0)  # SL
    inds = _inds(bars)
    bucket = bars.index[-1].to_pydatetime()
    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds, bucket=bucket))
    assert sig is not None and sig.payload["exit_reason"] == "SL"
    assert st.cooldown_until == bucket + timedelta(seconds=300)


# ─── 17. Crash override TP precedence ────────────────────────────────────


def test_tp_wins_over_be_when_both_could_fire():
    """+50 hits TP; pnl=50 > 0 so BE wouldn't fire anyway, but verifies ordering."""
    p = TradeStrat1KAIParams()
    _open_position(entry_price=100.0, sl_distance=50.0)
    bars = _bars(5, last_close=150.0)
    inds = _inds(bars)
    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.payload["exit_reason"] == "TP"


# ─── 18. Calibration flags (be_enabled / trail_(long|short)_enabled) ─────


def test_be_disabled_does_not_fire():
    """When be_enabled=False, the BE leg is skipped entirely. A LONG that
    peaks at +20 then pulls back to 0 must NOT close on BE; it stays open
    until SL / TRAIL / TIME / TP / CRASH."""
    p = TradeStrat1KAIParams(be_enabled=False)
    st = _open_position(entry_price=100.0, sl_distance=50.0)

    # Push peak to +20 (would arm BE in default config).
    bars1 = _bars(5, last_close=120.0)
    inds1 = _inds(bars1)
    TradeStrat1KAI(params=p).on_bar(_event(bars1, inds1))
    # Position still open, breakeven_armed should be False (skipped).
    assert st.position is not None
    assert st.position.breakeven_armed is False

    # Pull back to 0 — would have fired BE in default config.
    bars2 = _bars(5, last_close=100.0)
    inds2 = _inds(bars2)
    sig = TradeStrat1KAI(params=p).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    assert sig is None
    assert st.position is not None


def test_trail_long_disabled_blocks_long_trail():
    """LONG side with trail_long_enabled=False must NOT fire TRAIL even after
    arm + give_back conditions met."""
    p = TradeStrat1KAIParams(be_enabled=False, trail_long_enabled=False)
    st = _open_position(entry_price=100.0, sl_distance=100.0)
    bars1 = _bars(5, last_close=130.0)  # peak +30 (≥ arm 25)
    inds1 = _inds(bars1)
    TradeStrat1KAI(params=p).on_bar(_event(bars1, inds1))
    assert st.position is not None and st.position.peak_pnl == 30.0

    # Pullback to +12: in default config this fires TRAIL.
    bars2 = _bars(5, last_close=112.0)
    inds2 = _inds(bars2)
    sig = TradeStrat1KAI(params=p).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    assert sig is None
    assert st.position is not None


def test_trail_short_still_fires_when_long_disabled():
    """trail_long_enabled=False must not affect SHORT trail behavior."""
    p = TradeStrat1KAIParams(be_enabled=False, trail_long_enabled=False)
    st = mod._state_for(TradeStrat1KAI.name, SYM)
    st.position = _PositionState(
        side="SHORT", entry_price=200.0,
        entry_ts=DEFAULT_BAR_END,
        peak_pnl=0.0, sl_distance=100.0,
    )
    bars1 = _bars(5, last_close=170.0)  # SHORT peak +30
    inds1 = _inds(bars1)
    TradeStrat1KAI(params=p).on_bar(_event(bars1, inds1))
    assert st.position.peak_pnl == 30.0

    bars2 = _bars(5, last_close=188.0)  # SHORT pnl +12
    inds2 = _inds(bars2)
    sig = TradeStrat1KAI(params=p).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    assert sig is not None
    assert sig.payload["exit_reason"] == "TRAIL"


def test_validator_skips_be_vs_trail_check_when_be_disabled():
    """When be_enabled=False, the trail_arm > be_trigger geometry check is
    waived so callers can set them to any value without raising."""
    # Would have raised in default config — must NOT raise here.
    TradeStrat1KAIParams(
        be_enabled=False,
        be_trigger_points=999.0,  # nonsensical, but irrelevant since BE off
        trail_arm_points=25.0,
    )


# ─── 19. 1m BB-width pre-entry filter ─────────────────────────────────────


def _bb_helper_compute(n_bars: int, last_close: float, slope: float) -> float:
    """Return what _bb_width_pct would compute for a `_bars(n, last_close, slope)` ramp."""
    closes = np.linspace(last_close - slope * (n_bars - 1), last_close, n_bars)[-20:]
    sd = float(np.std(closes, ddof=0))
    return 4.0 * sd / float(closes[-1])


def test_bb_width_filter_blocks_entry_when_band_too_wide():
    """slope=2.0 over 30 bars gives BB-width ~0.27 — well above default 0.0035."""
    p = TradeStrat1KAIParams(bb_width_filter_enabled=True, bb_width_max_pct=0.0035)
    bars = _bars(30, last_close=200.0, slope=2.0)
    inds = _inds(bars)
    # Sanity: confirm width is indeed wide
    assert _bb_helper_compute(30, 200.0, 2.0) > 0.0035

    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    assert sig is None  # filter swallowed the entry


def test_bb_width_filter_allows_entry_when_band_tight():
    """slope=0.005 over 30 bars gives BB-width ~0.0001 — well under threshold."""
    p = TradeStrat1KAIParams(bb_width_filter_enabled=True, bb_width_max_pct=0.0035)
    bars = _bars(30, last_close=200.0, slope=0.005)
    inds = _inds(bars)
    assert _bb_helper_compute(30, 200.0, 0.005) < 0.0035

    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_bb_width_filter_disabled_does_not_block():
    """With the filter off (default), even a wide-band entry passes."""
    p = TradeStrat1KAIParams(bb_width_filter_enabled=False)
    bars = _bars(30, last_close=200.0, slope=2.0)
    inds = _inds(bars)
    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_bb_width_filter_skips_when_insufficient_history():
    """When bars are shorter than the window, the helper returns None and the
    filter does NOT block (fail-open). 5 bars < window=20."""
    p = TradeStrat1KAIParams(bb_width_filter_enabled=True, bb_width_max_pct=0.0035)
    bars = _bars(5, last_close=200.0)  # only 5 bars, window=20
    inds = _inds(bars)
    sig = TradeStrat1KAI(params=p).on_bar(_event(bars, inds))
    assert sig is not None  # would have entered without filter, still enters


def test_on_bar_intra_bar_sl_fills_at_target_long():
    """Bar mode: bar.low crosses LONG SL target → fill at the exact target,
    not at the (deeper) bar close. SL fires before TP/TRAIL even if bar
    high also reached TP (pessimistic ordering)."""
    _open_position(side="LONG", entry_price=200.0, sl_distance=30.0)

    closes = np.full(5, 200.0)
    highs = np.full(5, 200.0)
    lows = np.full(5, 200.0)
    # Last bar: close=195, high=260 (would have hit TP), low=160 (SL=170).
    closes[-1] = 195.0
    highs[-1] = 260.0
    lows[-1] = 160.0
    bars = pd.DataFrame({
        "open": closes, "high": highs, "low": lows, "close": closes,
        "tick_count": np.full(5, 1, dtype=int),
    }, index=pd.date_range(end=DEFAULT_BAR_END, periods=5, freq=FREQ))
    inds = _inds(bars)
    sig = TradeStrat1KAI(params=TradeStrat1KAIParams()).on_bar(
        _event(bars, inds, bucket=DEFAULT_BAR_END)
    )
    assert sig is not None and sig.side == "EXIT"
    # Pessimistic ordering: SL wins even though bar.high also crossed TP.
    assert sig.payload["exit_reason"] == "SL"
    assert sig.price == 170.0  # entry - sl_distance, NOT bar.low 160
    assert sig.payload["pnl_points"] == -30.0


def test_on_bar_intra_bar_sl_fills_at_target_short():
    """SHORT mirror: bar.high crosses SHORT SL target → fill at the exact
    target. Verifies the SHORT branch of the bar-aware exit logic."""
    _open_position(side="SHORT", entry_price=200.0, sl_distance=30.0)

    closes = np.full(5, 200.0)
    highs = np.full(5, 200.0)
    lows = np.full(5, 200.0)
    # SHORT SL target = 200 + 30 = 230. bar.high=240 crosses; close=205.
    closes[-1] = 205.0
    highs[-1] = 240.0
    lows[-1] = 195.0
    bars = pd.DataFrame({
        "open": closes, "high": highs, "low": lows, "close": closes,
        "tick_count": np.full(5, 1, dtype=int),
    }, index=pd.date_range(end=DEFAULT_BAR_END, periods=5, freq=FREQ))
    inds = _inds(bars)
    sig = TradeStrat1KAI(params=TradeStrat1KAIParams()).on_bar(
        _event(bars, inds, bucket=DEFAULT_BAR_END)
    )
    assert sig is not None and sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "SL"
    assert sig.price == 230.0  # entry + sl_distance
    assert sig.payload["pnl_points"] == -30.0
