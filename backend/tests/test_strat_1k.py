from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BarEvent, TickEvent
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

# Anchor bar end at 2026-05-01 03:00 UTC = 11:00 Taipei (inside the
# 09:15-12:15 entry window) so default fixtures fire entries cleanly.
DEFAULT_BAR_END = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def reset_state():
    _STATE.clear()
    yield
    _STATE.clear()


def _macd_5m_df(
    *, end: datetime, hist_curr: float = 1.0, n: int = 30
) -> pd.DataFrame:
    """Build a 5m MACD DataFrame whose last bar lands at `end - 1 * 5min` <
    `end` ≤ `last_5m_ts + 5min`, satisfying the 15-minute staleness guard
    for any tick within the next 5 minutes after the bucket close.
    """
    idx = pd.date_range(end=end, periods=n, freq="5min", tz="UTC")
    macd = np.full(n, hist_curr * 0.5, dtype=float)
    signal = np.zeros(n, dtype=float)
    hist = np.full(n, hist_curr, dtype=float)
    return pd.DataFrame(
        {"macd": macd, "signal": signal, "hist": hist}, index=idx
    )


def _kd_5m_df(
    *,
    end: datetime,
    k_prev: float = 70.0,
    d_prev: float = 60.0,
    k_curr: float = 80.0,
    d_curr: float = 65.0,
    n: int = 30,
) -> pd.DataFrame:
    """Build a 5m KD DataFrame whose last two rows expose the requested
    `k_prev / d_prev / k_curr / d_curr` so the 5m KD gate can be exercised
    deterministically. All earlier rows mirror the `*_prev` values.
    """
    idx = pd.date_range(end=end, periods=n, freq="5min", tz="UTC")
    k = np.full(n, k_prev, dtype=float)
    d = np.full(n, d_prev, dtype=float)
    if n >= 2:
        k[-2] = k_prev
        d[-2] = d_prev
    k[-1] = k_curr
    d[-1] = d_curr
    return pd.DataFrame({"k": k, "d": d}, index=idx)


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
    ma_prev: float = 100.0,
    ma_curr: float = 100.5,
    k_prev: float = 70.0,
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
    macd_5m: pd.DataFrame | None | str = "default",
    macd_5m_hist: float = 1.0,
    kd_5m: pd.DataFrame | None | str = "default",
    kd_5m_k_prev: float = 70.0,
    kd_5m_d_prev: float = 60.0,
    kd_5m_k_curr: float = 80.0,
    kd_5m_d_curr: float = 65.0,
) -> dict[str, pd.DataFrame]:
    """Build a primary-resolution indicator dict for `bars`.

    `macd_5m` controls the auxiliary 5m MACD frame:
      - "default" (sentinel) → fresh frame anchored at the last bar's
        timestamp so the staleness guard passes.
      - None → omit the key entirely (cold-start scenario).
      - explicit DataFrame → caller-supplied frame.

    `kd_5m` mirrors that contract for the 5m KD aux frame; the
    `kd_5m_*` knobs control the last-two-row K/D values when the
    "default" frame is used.
    """
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

    out: dict[str, pd.DataFrame] = {
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
    if isinstance(macd_5m, str) and macd_5m == "default":
        last_ts = bars.index[-1].to_pydatetime()
        out["macd_5m"] = _macd_5m_df(end=last_ts, hist_curr=macd_5m_hist)
    elif isinstance(macd_5m, pd.DataFrame):
        out["macd_5m"] = macd_5m
    # else: macd_5m is None → omit (cold-start path)
    if isinstance(kd_5m, str) and kd_5m == "default":
        last_ts = bars.index[-1].to_pydatetime()
        out["kd_5m"] = _kd_5m_df(
            end=last_ts,
            k_prev=kd_5m_k_prev,
            d_prev=kd_5m_d_prev,
            k_curr=kd_5m_k_curr,
            d_curr=kd_5m_d_curr,
        )
    elif isinstance(kd_5m, pd.DataFrame):
        out["kd_5m"] = kd_5m
    # else: kd_5m is None → omit (cold-start path)
    return out


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
    assert sig.payload["di_jump_points"] == 10.0


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
    inds = _inds(bars, k_prev=75.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_at_kd_boundary_below_floor():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=74.99)
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
    # Cooldown is now seconds-based (default 300s); cooldown_until is anchored
    # at the exit timestamp.
    assert st.cooldown_until == bucket2 + timedelta(seconds=300)
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


def test_cooldown_blocks_until_window_elapses():
    """Cooldown is seconds-based: blocks while ts < cooldown_until, then re-arms."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=50.0)  # pnl=−50 ≤ −sl=40 → SL.
    inds = _inds(bars)
    exit_ev = _event(bars, inds)
    sig_exit = TradeStrat1K(params=TradeStrat1KParams()).on_bar(exit_ev)
    assert sig_exit is not None and sig_exit.side == "EXIT"
    expected_cooldown_until = exit_ev.bucket + timedelta(seconds=300)
    assert st.cooldown_until == expected_cooldown_until
    assert st.position is None

    # Within the cooldown window: each bar suppressed; latch stays False.
    base_bucket = exit_ev.bucket
    bars_e = _bars(5, last_close=200.0)
    inds_e = _inds(bars_e)
    for offset_seconds in (60, 120, 180, 240, 299):
        ev = _event(bars_e, inds_e, bucket=base_bucket + timedelta(seconds=offset_seconds))
        sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(ev)
        assert sig is None
        assert st.cooldown_until == expected_cooldown_until
        assert st.position is None
        assert st.last_long_ready is False

    # Slip a non-firing event past the cooldown so cooldown_until clears + latch
    # re-arms cleanly without firing on the same bar (gates fail this round).
    release_bucket = base_bucket + timedelta(seconds=301)
    bars_low = _bars(5, last_close=200.0)
    inds_low = _inds(bars_low, hist_prev=0.5)
    sig_low = TradeStrat1K(params=TradeStrat1KParams()).on_bar(
        _event(bars_low, inds_low, bucket=release_bucket)
    )
    assert sig_low is None
    assert st.cooldown_until is None
    assert st.last_long_ready is False

    # Next aligned bar after cooldown clears → entry fires.
    fire_bucket = base_bucket + timedelta(seconds=360)
    sig_fire = TradeStrat1K(params=TradeStrat1KParams()).on_bar(
        _event(bars_e, inds_e, bucket=fire_bucket)
    )
    assert sig_fire is not None
    assert sig_fire.side == "LONG"


# ─── 12. DI_JUMP exit (strat_1k only) ────────────────────────────────────


def test_di_jump_fires_when_minus_di_jumps_above_threshold():
    """-DI 15 → 27 (jump=12 > 10) while position open → EXIT DI_JUMP_1M."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    # Close above entry but below TP (pnl=10, tp=50). SL/TRAIL also clear.
    bars = _bars(5, last_close=110.0)
    # -DI: prev=15, curr=27 → jump=12 > 10 → DI_JUMP fires.
    inds = _inds(bars, minus_prev=15.0, minus_curr=27.0, plus_curr=28.0)
    exit_ev = _event(bars, inds)
    sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(exit_ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "DI_JUMP_1M"
    assert sig.payload["pnl_points"] == 10.0
    assert st.position is None
    assert st.cooldown_until == exit_ev.bucket + timedelta(seconds=300)


def test_di_jump_no_fire_at_exact_threshold():
    """-DI jump = 10.0 exactly → no exit (strict `>`)."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=110.0)
    inds = _inds(bars, minus_prev=15.0, minus_curr=25.0)  # jump=10.0 exactly
    sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars, inds))

    assert sig is None
    assert st.position is not None


def test_di_jump_no_fire_below_threshold():
    """-DI jump = 9.9 → no exit."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=110.0)
    inds = _inds(bars, minus_prev=15.0, minus_curr=24.9)  # jump=9.9
    sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars, inds))

    assert sig is None
    assert st.position is not None


# ─── 13. on_tick path (tick-driven entries / exits / cooldown) ──────────


def test_on_tick_fires_entry_at_tick_ts():
    """on_tick fires LONG when gates align; Signal.ts == raw tick ts (mid-bucket)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    bucket = bars.index[-1].to_pydatetime()
    tick_ts = bucket + timedelta(seconds=17)
    # Tick price clears MA gate (MA curr = 100.5).
    ev = _tick_event(bars, inds, ts=tick_ts, price=205.0)

    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.side == "LONG"
    assert sig.ts == tick_ts
    assert sig.ts != bucket
    assert sig.price == 205.0
    assert sig.payload["fill_hint"] == "tick"


def test_on_tick_fires_tp_at_tick_price():
    """on_bar opens position; on_tick @ entry+tp_points+1 → EXIT TP at tick.ts."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"
    entry_price = sig_open.price

    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=23)
    tick_price = entry_price + 50.0 + 1.0  # tp_points = 50
    ev = _tick_event(bars, inds, ts=tick_ts, price=tick_price)
    sig = strat.on_tick(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"
    assert sig.ts == tick_ts
    assert sig.payload["fill_hint"] == "tick"

    st = _STATE[(TradeStrat1K.name, SYM)]
    assert st.position is None
    assert st.cooldown_until == tick_ts + timedelta(seconds=300)


def test_on_tick_cooldown_blocks_then_releases():
    """After exit at T: tick at T+10s blocked; tick at T+301s with gates fires."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)

    # Open + immediate TP exit at bucket close.
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None
    bucket = bars.index[-1].to_pydatetime()
    exit_tick_ts = bucket + timedelta(seconds=5)
    ev_exit = _tick_event(
        bars, inds, ts=exit_tick_ts, price=sig_open.price + 51.0
    )
    sig_exit = strat.on_tick(ev_exit)
    assert sig_exit is not None and sig_exit.side == "EXIT"

    # Tick during cooldown window → suppressed.
    early_ts = exit_tick_ts + timedelta(seconds=10)
    ev_early = _tick_event(bars, inds, ts=early_ts, price=205.0)
    sig_early = strat.on_tick(ev_early)
    assert sig_early is None

    # Tick after cooldown clears with gates aligned → entry fires.
    late_ts = exit_tick_ts + timedelta(seconds=301)
    ev_late = _tick_event(bars, inds, ts=late_ts, price=205.0)
    sig_late = strat.on_tick(ev_late)
    assert sig_late is not None
    assert sig_late.side == "LONG"
    assert sig_late.ts == late_ts


# ─── 14. 5m MACD gate (entry-only) ───────────────────────────────────────


def test_entry_blocked_when_macd_5m_missing():
    """Cold start: aux 5m frame absent → entry blocked even with all primary
    gates aligned."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m=None)
    assert "macd_5m" not in inds
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_when_macd_5m_empty():
    """Empty aux frame → entry blocked (defensive cold-start path)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    empty = pd.DataFrame(
        {"macd": [], "signal": [], "hist": []},
        index=pd.DatetimeIndex([], tz="UTC"),
    )
    inds = _inds(bars, macd_5m=empty)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_when_macd_5m_negative():
    """Aux 5m hist[-1] = -0.5 → entry blocked."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m_hist=-0.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_when_macd_5m_zero():
    """Aux 5m hist[-1] = 0.0 → entry blocked (strict `> 0`)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m_hist=0.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_fires_when_macd_5m_positive():
    """Aux 5m hist[-1] = 0.5 with primary gates aligned → entry fires."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m_hist=0.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_blocked_when_macd_5m_stale():
    """5m frame anchored 20 minutes before ts → staleness guard rejects."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    bucket = bars.index[-1].to_pydatetime()
    stale_5m = _macd_5m_df(end=bucket - timedelta(minutes=20), hist_curr=1.0)
    inds = _inds(bars, macd_5m=stale_5m)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_exit_ignores_macd_5m():
    """Open position → tick where 5m hist turns negative AND TP hit →
    EXIT TP still fires (5m gate is entry-only)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds_open = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds_open))
    assert sig_open is not None and sig_open.side == "LONG"

    # Now flip 5m MACD negative + send a tick at TP+1.
    bucket = bars.index[-1].to_pydatetime()
    tick_ts = bucket + timedelta(seconds=10)
    inds_exit = _inds(bars, macd_5m_hist=-0.5)
    ev_tick = _tick_event(
        bars, inds_exit, ts=tick_ts, price=sig_open.price + 51.0
    )
    sig_exit = strat.on_tick(ev_tick)
    assert sig_exit is not None
    assert sig_exit.side == "EXIT"
    assert sig_exit.payload["exit_reason"] == "TP"


# ─── 14b. 5m KD gate (entry-only) ────────────────────────────────────────


def test_entry_blocked_when_kd_5m_missing():
    """No `kd_5m` aux indicator → entry blocked."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, kd_5m=None)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_when_kd_5m_empty():
    """Empty `kd_5m` DataFrame → entry blocked."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, kd_5m=pd.DataFrame())
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_when_kd_5m_stale():
    """5m KD's last bar is older than 15 min vs the dispatch ts → blocked."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    bucket = bars.index[-1].to_pydatetime()
    stale_5m = _kd_5m_df(end=bucket - timedelta(minutes=20))
    inds = _inds(bars, kd_5m=stale_5m)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_when_kd_5m_k_at_floor():
    """Second 5m K exactly at floor (65) → blocked (strict `>`)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, kd_5m_k_curr=65.0, kd_5m_d_curr=60.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_when_kd_5m_k_below_d_prev():
    """K_prev ≤ D_prev (first bar fails K>D) → blocked."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, kd_5m_k_prev=55.0, kd_5m_d_prev=60.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_when_kd_5m_k_below_d_curr():
    """K_curr ≤ D_curr (second bar fails K>D) → blocked."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, kd_5m_k_curr=66.0, kd_5m_d_curr=70.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_fires_when_kd_5m_passes():
    """All four conditions met (K_prev>D_prev, K_curr>D_curr, K_curr>floor)
    → entry allowed (assuming all other gates aligned via defaults)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(
        bars,
        kd_5m_k_prev=66.0, kd_5m_d_prev=60.0,
        kd_5m_k_curr=80.0, kd_5m_d_curr=65.0,
    )
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_exit_ignores_kd_5m():
    """Open position; flip 5m KD K below floor + send tick at TP+1 →
    EXIT TP still fires (5m KD gate is entry-only)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds_open = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds_open))
    assert sig_open is not None and sig_open.side == "LONG"

    bucket = bars.index[-1].to_pydatetime()
    tick_ts = bucket + timedelta(seconds=10)
    inds_exit = _inds(bars, kd_5m_k_curr=50.0, kd_5m_d_curr=55.0)
    ev_tick = _tick_event(
        bars, inds_exit, ts=tick_ts, price=sig_open.price + 51.0
    )
    sig_exit = strat.on_tick(ev_tick)
    assert sig_exit is not None
    assert sig_exit.side == "EXIT"
    assert sig_exit.payload["exit_reason"] == "TP"


# ─── 15. Entry-window gate ────────────────────────────────────────────────


def _bars_at_taipei(taipei_hms: tuple[int, int, int]) -> pd.DataFrame:
    """Build bars whose last index lands at the requested Taipei wall time
    (Asia/Taipei is UTC+8 with no DST, so we subtract 8h to land in UTC)."""
    h, m, s = taipei_hms
    end_utc = datetime(2026, 5, 1, h, m, s, tzinfo=UTC) - timedelta(hours=8)
    return _bars(5, last_close=200.0, end=end_utc)


def test_entry_blocked_at_13_00():
    """13:00 Taipei (inside 12:15–21:00 closed gap) → entry blocked."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((13, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_allowed_at_11_00():
    """11:00 Taipei → entry allowed."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((11, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_blocked_at_16_00():
    """16:00 Taipei (was night-allowed pre-window-narrowing) → blocked."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((16, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_allowed_at_22_00():
    """22:00 Taipei → entry allowed (inside [21:00, 24:00))."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((22, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_blocked_at_03_00():
    """03:00 Taipei (post-midnight overnight) → entry blocked per strict
    midnight cutoff."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((3, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_window_boundary_open():
    """09:15:00 Taipei → entry allowed (inclusive lower bound)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((9, 15, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_window_boundary_close_exclusive():
    """12:15:00 Taipei → entry blocked (exclusive upper bound)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((12, 15, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_at_15_00():
    """15:00:00 Taipei (post-narrowing: blocked, no longer in window)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((15, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_at_20_59_59():
    """20:59:59 Taipei → blocked (one second before night-window open)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((20, 59, 59))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_window_boundary_21_00():
    """21:00:00 Taipei → entry allowed (inclusive lower bound of night)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((21, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_exit_runs_outside_window():
    """Open position via on_bar at 11:00 Taipei. Subsequent tick at 13:00
    Taipei with TP hit → EXIT fires (window gate is entry-only)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((11, 0, 0))
    inds_open = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds_open))
    assert sig_open is not None and sig_open.side == "LONG"

    tick_ts = datetime(2026, 5, 1, 13, 0, 0, tzinfo=UTC) - timedelta(hours=8)
    inds_exit = _inds(bars)
    ev_tick = _tick_event(
        bars, inds_exit, ts=tick_ts, price=sig_open.price + 51.0
    )
    sig_exit = strat.on_tick(ev_tick)
    assert sig_exit is not None
    assert sig_exit.side == "EXIT"
    assert sig_exit.payload["exit_reason"] == "TP"


def test_window_reopen_fires_after_block():
    """12:14:59 (window open, gates align) → LONG fires.
    Flush position; 12:15:00 (window closed, gates STILL aligned) → None
    (latch reset by window block); 21:00:00 (window reopens, gates STILL
    aligned) → LONG fires (rising-edge re-detection post-reset)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())

    # Stage 1: 12:14:59 Taipei → first LONG fires.
    bars1 = _bars_at_taipei((12, 14, 59))
    inds1 = _inds(bars1)
    sig1 = strat.on_bar(_event(bars1, inds1))
    assert sig1 is not None and sig1.side == "LONG"

    # Flush the position (simulate immediate exit) so we can re-test entry.
    st = _STATE[(TradeStrat1K.name, SYM)]
    st.position = None
    st.cooldown_until = None
    # last_long_ready stays True after the entry fires above.
    assert st.last_long_ready is True

    # Stage 2: 12:15:00 (window closed, gates still aligned) → None,
    # AND latch resets to False so the window reopen fires as a fresh edge.
    bars2 = _bars_at_taipei((12, 15, 0))
    inds2 = _inds(bars2)
    sig2 = strat.on_bar(_event(bars2, inds2))
    assert sig2 is None
    assert st.last_long_ready is False

    # Stage 3: 21:00:00 (window reopens, gates still aligned) → LONG fires.
    bars3 = _bars_at_taipei((21, 0, 0))
    inds3 = _inds(bars3)
    sig3 = strat.on_bar(_event(bars3, inds3))
    assert sig3 is not None
    assert sig3.side == "LONG"
