from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BarEvent, TickEvent
from app.strategies.examples import strat_15k as mod
from app.strategies.examples.strat_15k import (
    _STATE,
    TradeStrat15K,
    TradeStrat15KParams,
    _PositionState,
    _StratState,
)

RES = "15m"
FREQ = "15min"
SYM = "MXF"
TPE = ZoneInfo("Asia/Taipei")
# 16:00 Asia/Taipei is inside the night entry window [15:00, 24:00) — UTC 08:00.
# Using 16:00 leaves > 7 hours of in-window time for cooldown-release tests
# that advance the timestamp by ~75min (4500s) without crossing the window.
DEFAULT_END_UTC = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)


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
    end_ts: datetime | None = None,
) -> pd.DataFrame:
    end = end_ts or DEFAULT_END_UTC
    idx = pd.date_range(end=end, periods=n, freq=FREQ)
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
    k_curr: float = 73.0,
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


def _macd_5m(
    *,
    end_ts: datetime,
    n: int = 5,
    hist_curr: float = 0.5,
    macd_curr: float = 0.5,
    signal_curr: float = 0.0,
) -> pd.DataFrame:
    """Build a TZ-aware 5m MACD indicator DataFrame ending at ``end_ts``."""
    idx = pd.date_range(end=end_ts, periods=n, freq="5min")
    return pd.DataFrame(
        {
            "macd": np.full(n, macd_curr, dtype=float),
            "signal": np.full(n, signal_curr, dtype=float),
            "hist": np.full(n, hist_curr, dtype=float),
        },
        index=idx,
    )


def _event(
    bars: pd.DataFrame,
    inds: dict[str, pd.DataFrame],
    bucket=None,
    *,
    aux_macd_5m: pd.DataFrame | None = None,
    inject_aux: bool = True,
) -> BarEvent:
    bucket_ts = bucket or bars.index[-1].to_pydatetime()
    payload = dict(inds)
    if inject_aux and "macd_5m" not in payload:
        # Default: aux 5m MACD positive at the bucket time so primary gates
        # are decisive unless a test overrides via aux_macd_5m or
        # inject_aux=False.
        payload["macd_5m"] = aux_macd_5m if aux_macd_5m is not None else (
            _macd_5m(end_ts=bucket_ts)
        )
    elif aux_macd_5m is not None:
        payload["macd_5m"] = aux_macd_5m
    return BarEvent(
        symbol=SYM,
        resolution=RES,
        bucket=bucket_ts,
        bars=bars,
        indicators=payload,
    )


def _tick_event(
    bars: pd.DataFrame,
    inds: dict[str, pd.DataFrame],
    *,
    ts: datetime,
    price: float,
    aux_macd_5m: pd.DataFrame | None = None,
    inject_aux: bool = True,
) -> TickEvent:
    payload = dict(inds)
    if inject_aux and "macd_5m" not in payload:
        payload["macd_5m"] = aux_macd_5m if aux_macd_5m is not None else (
            _macd_5m(end_ts=ts)
        )
    elif aux_macd_5m is not None:
        payload["macd_5m"] = aux_macd_5m
    return TickEvent(
        symbol=SYM,
        resolution=RES,
        ts=ts,
        price=price,
        bars=bars,
        indicators=payload,
    )


# ─── 1. entry happy path ─────────────────────────────────────────────────


def test_entry_happy_path():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))

    assert sig is not None
    assert sig.side == "LONG"
    assert sig.price == 200.0
    snap = sig.payload["entry_ind"]
    for key in ("k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx"):
        assert key in snap
    assert sig.payload["tp_points"] == 130.0
    assert sig.payload["sl_points"] == 70.0
    assert sig.payload["trail_points"] == 80.0
    assert sig.payload["fill_hint"] == "tick"


# ─── 2. MA fail ──────────────────────────────────────────────────────────


def test_no_entry_when_close_not_above_ma():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=100.0)
    inds = _inds(bars, ma_prev=100.0, ma_curr=100.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 3. MA flat ──────────────────────────────────────────────────────────


def test_no_entry_when_ma_flat():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, ma_prev=100.0, ma_curr=100.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 4. KD floor (now 75) ────────────────────────────────────────────────


def test_no_entry_when_first_k_at_floor():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=75.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_at_kd_boundary_below_floor():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=74.99)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_no_entry_when_first_k_above_new_floor():
    """KD floor lowered 80 → 75: K=78 (passed under old spec) now blocks."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=78.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 5. MACD wrong sign ──────────────────────────────────────────────────


def test_no_entry_when_hist_prev_positive():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_prev=0.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_hist_curr_zero():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_curr=0.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 6. DMI not flipping ─────────────────────────────────────────────────


def test_no_entry_when_minus_di_flat():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, minus_prev=18.0, minus_curr=18.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 7. rising-edge ──────────────────────────────────────────────────────


def test_rising_edge_suppresses_when_already_ready():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    _STATE[(TradeStrat15K.name, SYM)] = _StratState(last_long_ready=True)
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None
    st = _STATE[(TradeStrat15K.name, SYM)]
    assert st.position is None
    assert st.last_long_ready is True


# ─── 8. TP exit ──────────────────────────────────────────────────────────


def test_tp_exit_at_threshold():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig1 = strat.on_bar(_event(bars, inds))
    assert sig1 is not None and sig1.side == "LONG"

    bars2 = _bars(5, last_close=200.0 + 130.0)
    bucket2 = bars2.index[-1].to_pydatetime() + pd.Timedelta(minutes=15)
    inds2 = _inds(bars2)
    sig2 = TradeStrat15K(params=TradeStrat15KParams()).on_bar(
        _event(bars2, inds2, bucket=bucket2)
    )
    assert sig2 is not None
    assert sig2.side == "EXIT"
    assert sig2.payload["exit_reason"] == "TP"
    assert sig2.payload["pnl_points"] == 130.0
    assert sig2.payload["fill_hint"] == "tick"

    st = _STATE[(TradeStrat15K.name, SYM)]
    assert st.position is None
    # Cooldown is now seconds-based (default 4500s = 5 × 15m).
    assert st.cooldown_until == bucket2 + timedelta(seconds=4500)
    assert st.last_long_ready is False


# ─── 9. SL exit ──────────────────────────────────────────────────────────


def test_sl_exit_at_threshold():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    strat.on_bar(_event(bars, inds))

    bars2 = _bars(5, last_close=200.0 - 70.0)
    bucket2 = bars.index[-1].to_pydatetime() + pd.Timedelta(minutes=15)
    inds2 = _inds(bars2)
    sig2 = TradeStrat15K(params=TradeStrat15KParams()).on_bar(
        _event(bars2, inds2, bucket=bucket2)
    )
    assert sig2 is not None
    assert sig2.side == "EXIT"
    assert sig2.payload["exit_reason"] == "SL"
    assert sig2.payload["pnl_points"] == -70.0


# ─── 10. trailing stop ───────────────────────────────────────────────────


def test_trail_exit_after_peak():
    """Seed open position at 100; tp=130, sl=70, trail=80."""
    st = mod._state_for(TradeStrat15K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars2 = _bars(5, last_close=150.0)
    inds2 = _inds(bars2)
    sig2 = TradeStrat15K(params=TradeStrat15KParams()).on_bar(_event(bars2, inds2))
    assert sig2 is None
    assert st.position.peak_pnl == 50.0

    bars3 = _bars(5, last_close=80.0)
    inds3 = _inds(bars3)
    sig3 = TradeStrat15K(params=TradeStrat15KParams()).on_bar(_event(bars3, inds3))
    assert sig3 is None
    assert st.position.peak_pnl == 50.0

    bars4 = _bars(5, last_close=70.0)
    inds4 = _inds(bars4)
    sig4 = TradeStrat15K(params=TradeStrat15KParams()).on_bar(_event(bars4, inds4))
    assert sig4 is not None
    assert sig4.side == "EXIT"
    assert sig4.payload["exit_reason"] == "TRAIL"
    assert sig4.payload["pnl_points"] == -30.0


# ─── 11. cooldown (seconds-based) ────────────────────────────────────────


def test_cooldown_blocks_until_window_elapses():
    """Cooldown is seconds-based: blocks while ts < cooldown_until, then re-arms."""
    st = mod._state_for(TradeStrat15K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=30.0)  # pnl=−70 ≤ −sl=70 → SL.
    inds = _inds(bars)
    exit_ev = _event(bars, inds)
    sig_exit = TradeStrat15K(params=TradeStrat15KParams()).on_bar(exit_ev)
    assert sig_exit is not None and sig_exit.side == "EXIT"
    expected_cooldown_until = exit_ev.bucket + timedelta(seconds=4500)
    assert st.cooldown_until == expected_cooldown_until
    assert st.position is None

    # Within the cooldown window: each event suppressed; latch stays False.
    base_bucket = exit_ev.bucket
    bars_e = _bars(5, last_close=200.0)
    inds_e = _inds(bars_e)
    for offset_seconds in (900, 1800, 2700, 3600, 4499):
        ev = _event(
            bars_e, inds_e,
            bucket=base_bucket + timedelta(seconds=offset_seconds),
        )
        sig = TradeStrat15K(params=TradeStrat15KParams()).on_bar(ev)
        assert sig is None
        assert st.cooldown_until == expected_cooldown_until
        assert st.position is None
        assert st.last_long_ready is False

    # Slip a non-firing event past the cooldown so cooldown_until clears +
    # latch re-arms cleanly without firing on the same bar.
    release_bucket = base_bucket + timedelta(seconds=4501)
    bars_low = _bars(5, last_close=200.0)
    inds_low = _inds(bars_low, hist_prev=0.5)
    sig_low = TradeStrat15K(params=TradeStrat15KParams()).on_bar(
        _event(bars_low, inds_low, bucket=release_bucket)
    )
    assert sig_low is None
    assert st.cooldown_until is None
    assert st.last_long_ready is False

    # Next aligned event after cooldown clears → entry fires.
    fire_bucket = base_bucket + timedelta(seconds=5400)
    sig_fire = TradeStrat15K(params=TradeStrat15KParams()).on_bar(
        _event(bars_e, inds_e, bucket=fire_bucket)
    )
    assert sig_fire is not None
    assert sig_fire.side == "LONG"


# ─── 12. on_tick path (tick-driven entries / exits / cooldown) ──────────


def test_on_tick_fires_entry_at_tick_ts():
    """on_tick fires LONG when gates align; Signal.ts == raw tick ts (mid-bucket)."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    bucket = bars.index[-1].to_pydatetime()
    tick_ts = bucket + timedelta(seconds=137)
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
    """on_bar opens position; on_tick @ entry+tp+1 → EXIT TP at tick.ts."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"
    entry_price = sig_open.price

    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=233)
    tick_price = entry_price + 130.0 + 1.0  # tp_points = 130
    ev = _tick_event(bars, inds, ts=tick_ts, price=tick_price)
    sig = strat.on_tick(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"
    assert sig.ts == tick_ts
    assert sig.payload["fill_hint"] == "tick"

    st = _STATE[(TradeStrat15K.name, SYM)]
    assert st.position is None
    assert st.cooldown_until == tick_ts + timedelta(seconds=4500)


def test_on_tick_cooldown_blocks_then_releases():
    """After exit at T: tick at T+10s blocked; tick at T+4501s with gates fires."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)

    # Open + immediate TP exit at a mid-bucket tick.
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None
    bucket = bars.index[-1].to_pydatetime()
    exit_tick_ts = bucket + timedelta(seconds=5)
    ev_exit = _tick_event(
        bars, inds, ts=exit_tick_ts, price=sig_open.price + 131.0
    )
    sig_exit = strat.on_tick(ev_exit)
    assert sig_exit is not None and sig_exit.side == "EXIT"

    # Tick during cooldown window → suppressed.
    early_ts = exit_tick_ts + timedelta(seconds=10)
    ev_early = _tick_event(bars, inds, ts=early_ts, price=205.0)
    sig_early = strat.on_tick(ev_early)
    assert sig_early is None

    # Tick after cooldown clears with gates aligned → entry fires.
    late_ts = exit_tick_ts + timedelta(seconds=4501)
    ev_late = _tick_event(
        bars, inds, ts=late_ts, price=205.0,
        aux_macd_5m=_macd_5m(end_ts=late_ts),
    )
    sig_late = strat.on_tick(ev_late)
    assert sig_late is not None
    assert sig_late.side == "LONG"
    assert sig_late.ts == late_ts


# ─── 13. 5m MACD aux gate (entry-only) ───────────────────────────────────


def test_no_entry_when_macd_5m_missing():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    # Build event with no aux indicator at all.
    ev = _event(bars, inds, inject_aux=False)
    assert "macd_5m" not in ev.indicators
    sig = strat.on_bar(ev)
    assert sig is None


def test_no_entry_when_macd_5m_empty():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    ev = _event(
        bars, inds, inject_aux=False,
        aux_macd_5m=pd.DataFrame(),
    )
    sig = strat.on_bar(ev)
    assert sig is None


def test_no_entry_when_macd_5m_negative():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    bucket = bars.index[-1].to_pydatetime()
    ev = _event(
        bars, inds,
        aux_macd_5m=_macd_5m(end_ts=bucket, hist_curr=-0.1),
    )
    sig = strat.on_bar(ev)
    assert sig is None


def test_no_entry_when_macd_5m_zero():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    bucket = bars.index[-1].to_pydatetime()
    ev = _event(
        bars, inds,
        aux_macd_5m=_macd_5m(end_ts=bucket, hist_curr=0.0),
    )
    sig = strat.on_bar(ev)
    assert sig is None


def test_no_entry_when_macd_5m_stale_beyond_15min():
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    bucket = bars.index[-1].to_pydatetime()
    # Aux ends 16 min before the bucket → staleness guard blocks.
    stale_end = bucket - timedelta(minutes=16)
    ev = _event(
        bars, inds,
        aux_macd_5m=_macd_5m(end_ts=stale_end, hist_curr=0.5),
    )
    sig = strat.on_bar(ev)
    assert sig is None


def test_entry_fires_when_macd_5m_within_staleness_window():
    """Aux ends just inside the 15-minute staleness boundary (half-open).

    Exactly 15 minutes is treated as stale (matches the ingest watchdog's
    `>= 3 * delta` retire threshold), so we use 14m59s here to assert the
    fresh-side of the boundary still fires.
    """
    strat = TradeStrat15K(params=TradeStrat15KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    bucket = bars.index[-1].to_pydatetime()
    fresh_end = bucket - timedelta(minutes=14, seconds=59)
    ev = _event(
        bars, inds,
        aux_macd_5m=_macd_5m(end_ts=fresh_end, hist_curr=0.5),
    )
    sig = strat.on_bar(ev)
    assert sig is not None
    assert sig.side == "LONG"


def test_exit_ignores_macd_5m_gate():
    """With an open position, a tick where 5m MACD is negative still TPs."""
    st = mod._state_for(TradeStrat15K.name, SYM)
    bucket = DEFAULT_END_UTC
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=bucket - timedelta(minutes=15),
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=230.0)  # pnl = +130 (TP)
    inds = _inds(bars)
    ev = _event(
        bars, inds,
        aux_macd_5m=_macd_5m(end_ts=bucket, hist_curr=-1.0),  # negative
    )
    sig = TradeStrat15K(params=TradeStrat15KParams()).on_bar(ev)
    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"


# ─── 14. entry-window gate ───────────────────────────────────────────────


def _utc_for_taipei(year, month, day, hour, minute=0, second=0, microsecond=0):
    """Build a UTC-aware datetime that maps to the given Taipei wall clock."""
    local = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=TPE)
    return local.astimezone(UTC)


def test_no_entry_at_13_00_taipei():
    """13:00 Taipei (between 12:15 and 15:00) → entry blocked."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    end_ts = _utc_for_taipei(2026, 5, 1, 13, 0)
    bars = _bars(5, last_close=200.0, end_ts=end_ts)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_at_03_00_taipei_overnight():
    """03:00 Taipei (post-midnight) → entry blocked even though TAIFEX is open."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    end_ts = _utc_for_taipei(2026, 5, 2, 3, 0)
    bars = _bars(5, last_close=200.0, end_ts=end_ts)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_at_12_15_00_taipei_boundary():
    """12:15:00.000 Taipei → entry blocked (half-open interval)."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    end_ts = _utc_for_taipei(2026, 5, 1, 12, 15, 0)
    bars = _bars(5, last_close=200.0, end_ts=end_ts)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_at_11_00_taipei():
    """11:00 Taipei → entry allowed."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    end_ts = _utc_for_taipei(2026, 5, 1, 11, 0)
    bars = _bars(5, last_close=200.0, end_ts=end_ts)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_at_16_00_taipei():
    """16:00 Taipei → entry allowed (night session)."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    end_ts = _utc_for_taipei(2026, 5, 1, 16, 0)
    bars = _bars(5, last_close=200.0, end_ts=end_ts)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_at_09_15_00_taipei_open():
    """09:15:00 Taipei → first allowed instant of the day window."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    end_ts = _utc_for_taipei(2026, 5, 1, 9, 15, 0)
    bars = _bars(5, last_close=200.0, end_ts=end_ts)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_at_15_00_00_taipei_open():
    """15:00:00 Taipei → first allowed instant of the night window."""
    strat = TradeStrat15K(params=TradeStrat15KParams())
    end_ts = _utc_for_taipei(2026, 5, 1, 15, 0, 0)
    bars = _bars(5, last_close=200.0, end_ts=end_ts)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_exits_run_at_13_00_taipei():
    """Open position at 11:50 still closes at 13:00 (window gate is entry-only)."""
    st = mod._state_for(TradeStrat15K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=_utc_for_taipei(2026, 5, 1, 11, 50),
        peak_pnl=0.0,
    )

    end_ts = _utc_for_taipei(2026, 5, 1, 13, 0)
    bars = _bars(5, last_close=230.0, end_ts=end_ts)  # pnl=+130 → TP
    inds = _inds(bars)
    sig = TradeStrat15K(params=TradeStrat15KParams()).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"


def test_window_reopen_rising_edge():
    """Gates align before 12:15; latch reset at 12:15 close; entry fires at 15:00 reopen."""
    strat = TradeStrat15K(params=TradeStrat15KParams())

    # 12:00 Taipei — gates align, fires LONG.
    end_a = _utc_for_taipei(2026, 5, 1, 12, 0)
    bars_a = _bars(5, last_close=200.0, end_ts=end_a)
    inds_a = _inds(bars_a)
    sig_a = strat.on_bar(_event(bars_a, inds_a))
    assert sig_a is not None and sig_a.side == "LONG"
    # Reset to compare reopen behaviour without an open position interfering.
    _STATE.clear()

    # 12:14 Taipei (still allowed) — gates align, latch goes True.
    end_b = _utc_for_taipei(2026, 5, 1, 12, 14)
    bars_b = _bars(5, last_close=200.0, end_ts=end_b)
    inds_b = _inds(bars_b)
    sig_b = strat.on_bar(_event(bars_b, inds_b))
    assert sig_b is not None and sig_b.side == "LONG"
    # Force-reset position so the next bar evaluates the entry path again.
    st = _STATE[(TradeStrat15K.name, SYM)]
    st.position = None
    assert st.last_long_ready is True

    # 12:15 Taipei — window blocks, latch resets so reopen fires fresh edge.
    end_c = _utc_for_taipei(2026, 5, 1, 12, 15)
    bars_c = _bars(5, last_close=200.0, end_ts=end_c)
    inds_c = _inds(bars_c)
    sig_c = strat.on_bar(_event(bars_c, inds_c))
    assert sig_c is None
    assert st.last_long_ready is False

    # 15:00 Taipei reopen — gates still aligned → fires immediately.
    end_d = _utc_for_taipei(2026, 5, 1, 15, 0)
    bars_d = _bars(5, last_close=200.0, end_ts=end_d)
    inds_d = _inds(bars_d)
    sig_d = strat.on_bar(_event(bars_d, inds_d))
    assert sig_d is not None
    assert sig_d.side == "LONG"
