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
    _exit_params_for,
    _PositionState,
    _StratState,
)

RES = "1m"
FREQ = "1min"
SYM = "MXF"

# Anchor bar end at 2026-05-01 01:00 UTC = 09:00 Taipei (inside the new
# 08:45-13:45 entry window, in the [08:45, 10:31) TP=50 bucket).
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
    k_prev: float = 60.0,  # < kd_long_floor = 70
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
    dmi_5m_plus: float = 30.0,
    dmi_5m_minus: float = 15.0,
) -> dict[str, pd.DataFrame]:
    """Build a primary-resolution indicator dict for `bars`.

    Defaults satisfy every entry gate:
      1m DMI rising  (+DI 25→28, -DI 18→12)
      1m KD          (k_prev=60<70, k>d both rows)
      1m MACD hist   (hist_curr=1.0 > 0)
      5m DMI align   (dmi_5m +DI=30 > -DI=15 → di_positive gate passes)

    The current source ships a 4th entry gate ``require_5m_alignment=
    "di_positive"`` (default), which consults the most-recent CLOSED 5m
    bar via the aux key ``"dmi_5m"`` and blocks LONG unless 5m +DI > -DI.
    We therefore inject a ``dmi_5m`` aux DataFrame whose single closed
    5m bar (anchored 5 minutes before the last 1m bar so the close-trim
    in ``_five_min_aligned`` retains it) has +DI > -DI by default.
    """
    n = len(bars)
    idx = bars.index

    # 5m alignment aux. Anchor the bar end at `bars.index[-1] - 5min` so
    # `index + 5min <= ts` holds for any tick ts >= bars.index[-1].
    dmi_5m_end = bars.index[-1] - pd.Timedelta(minutes=5)
    dmi_5m_idx = pd.date_range(end=dmi_5m_end, periods=3, freq="5min")
    dmi_5m_df = pd.DataFrame(
        {
            "plus_di": np.full(len(dmi_5m_idx), dmi_5m_plus, dtype=float),
            "minus_di": np.full(len(dmi_5m_idx), dmi_5m_minus, dtype=float),
            "adx": np.full(len(dmi_5m_idx), 25.0, dtype=float),
        },
        index=dmi_5m_idx,
    )

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
        "dmi_5m": dmi_5m_df,
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


def _bars_at_taipei(taipei_hms: tuple[int, int, int]) -> pd.DataFrame:
    """Build bars whose last index lands at the requested Taipei wall time."""
    h, m, s = taipei_hms
    end_utc = datetime(2026, 5, 1, h, m, s, tzinfo=UTC) - timedelta(hours=8)
    return _bars(5, last_close=200.0, end=end_utc)


def _taipei_ts(hms: tuple[int, int, int]) -> datetime:
    h, m, s = hms
    return datetime(2026, 5, 1, h, m, s, tzinfo=UTC) - timedelta(hours=8)


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
    # 09:00 Taipei → [08:45, 10:31) bucket → TP 50 / TRAIL 40
    assert sig.payload["tp_points"] == 50.0
    assert sig.payload["trail_points"] == 40.0
    assert "sl_points" not in sig.payload
    assert "di_jump_points" not in sig.payload


def test_aux_indicator_specs_declares_5m_gates():
    """Current source declares 5m aux indicators (vrebound + alignment gates).

    The ``dmi_5m`` aux backs the default ``require_5m_alignment="di_positive"``
    gate; the others are precomputed for the alternative alignment modes /
    the optional V-rebound gate.
    """
    aux = TradeStrat1K.aux_indicator_specs
    assert set(aux) == {"vrebound_5m", "macd_5m", "dmi_5m", "ma_5m"}
    for spec in aux.values():
        assert spec["resolution"] == "5m"


# ─── 2. KD floor ─────────────────────────────────────────────────────────


def test_no_entry_when_first_k_at_floor():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=70.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_at_kd_boundary_below_floor():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=69.99)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_no_entry_when_kd_not_above():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=40.0, d_prev=50.0)  # k_prev < d_prev
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_curr_kd_not_above():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_curr=58.0, d_curr=60.0)  # k_curr < d_curr
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 3. MACD histogram gate ──────────────────────────────────────────────


def test_no_entry_when_hist_zero():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_curr=0.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_hist_negative():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_curr=-0.01)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_when_hist_just_above_zero():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, hist_curr=0.01)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


# ─── 4. DMI rising mandatory ─────────────────────────────────────────────


def test_no_entry_when_plus_di_flat():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, plus_prev=28.0, plus_curr=28.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_plus_di_falling():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, plus_prev=30.0, plus_curr=29.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_minus_di_flat():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, minus_prev=18.0, minus_curr=18.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_minus_di_rising():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, minus_prev=18.0, minus_curr=20.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


# ─── 5. rising-edge latch ────────────────────────────────────────────────


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


# ─── 6. ToD-segmented TP ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hms,expected_tp",
    [
        ((9, 0, 0), 50.0),     # [08:45, 10:31)
        ((10, 30, 59), 50.0),  # last second of [08:45, 10:31)
        ((10, 31, 0), 40.0),   # boundary into [10:31, 13:45)
        ((11, 0, 0), 40.0),
        ((13, 44, 59), 40.0),  # last second of day session
        ((15, 0, 0), 30.0),    # night [15:00, 18:01)
        ((16, 0, 0), 30.0),
        ((18, 0, 59), 30.0),
        ((18, 1, 0), 50.0),    # boundary into [18:01, 23:31)
        ((22, 0, 0), 50.0),
        ((23, 30, 59), 50.0),
        ((23, 31, 0), 30.0),   # overnight bucket starts
        ((23, 59, 59), 30.0),
        ((0, 30, 0), 30.0),    # past-midnight overnight
        ((4, 59, 59), 30.0),   # last second of overnight
    ],
)
def test_exit_params_for_buckets(hms, expected_tp):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Taipei")
    ts = datetime(2026, 5, 1, *hms, tzinfo=tz)
    tp, trail = _exit_params_for(ts, tz)
    assert tp == expected_tp
    assert trail == 40.0


def test_exit_params_closed_gap_fallback():
    """Closed gaps [13:45,15:00) and [05:00,08:45) → fallback (40, 40)."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Taipei")
    for hms in ((14, 0, 0), (14, 59, 59), (5, 0, 0), (8, 44, 59)):
        ts = datetime(2026, 5, 1, *hms, tzinfo=tz)
        tp, trail = _exit_params_for(ts, tz)
        assert tp == 40.0
        assert trail == 40.0


def test_tp_exit_uses_tod_bucket_50pt_morning():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((9, 0, 0))
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"

    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=10)
    ev = _tick_event(bars, inds, ts=tick_ts, price=sig_open.price + 50.0)
    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"
    assert sig.payload["pnl_points"] == 50.0


def test_tp_exit_uses_tod_bucket_30pt_evening():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((16, 0, 0))
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"
    assert sig_open.payload["tp_points"] == 30.0

    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=10)
    # +29.99 must NOT fire; +30 must fire.
    ev_short = _tick_event(
        bars, inds, ts=tick_ts, price=sig_open.price + 29.99
    )
    sig_short = strat.on_tick(ev_short)
    assert sig_short is None

    ev = _tick_event(bars, inds, ts=tick_ts, price=sig_open.price + 30.0)
    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"
    assert sig.payload["pnl_points"] == 30.0


def test_tp_exit_uses_tod_bucket_40pt_late_morning():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((11, 0, 0))
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"
    assert sig_open.payload["tp_points"] == 40.0

    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=10)
    ev = _tick_event(bars, inds, ts=tick_ts, price=sig_open.price + 40.0)
    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.payload["exit_reason"] == "TP"
    assert sig.payload["pnl_points"] == 40.0


def test_tp_evaluated_against_current_ts_not_entry_ts():
    """Open at 18:00 (TP=30); price spikes past +40 at 18:30 (TP=50).

    The 18:00 → 18:01 boundary crossing means the TOD-resolved TP at the
    exit tick is 50, so a +30 spike at 18:30 must NOT fire TP. (Open
    payload still records tp_points=30 from entry time.)
    """
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((18, 0, 0))
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"
    assert sig_open.payload["tp_points"] == 30.0

    tick_ts = _taipei_ts((18, 30, 0))
    ev = _tick_event(bars, inds, ts=tick_ts, price=sig_open.price + 30.0)
    sig = strat.on_tick(ev)
    assert sig is None  # 18:30 → bucket TP=50, +30 not enough


# ─── 7. TRAIL exit ───────────────────────────────────────────────────────


def test_trail_exit_after_peak():
    """Peak rises +30 then drops to peak-40 → TRAIL fires (no SL)."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=DEFAULT_BAR_END,
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=125.0)
    inds = _inds(bars)
    sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars, inds))
    assert sig is None
    assert st.position is not None
    assert st.position.peak_pnl == 25.0

    bars2 = _bars(5, last_close=84.0)  # pnl = -16 → peak(25) - 16 = 41 < 0; trail 40
    inds2 = _inds(bars2)
    sig2 = TradeStrat1K(params=TradeStrat1KParams()).on_bar(
        _event(bars2, inds2, bucket=DEFAULT_BAR_END + timedelta(minutes=2))
    )
    assert sig2 is not None
    assert sig2.side == "EXIT"
    assert sig2.payload["exit_reason"] == "TRAIL"


def test_trail_fires_on_immediate_drawdown_no_sl():
    """No hard SL: a 100-pt drawdown from entry (peak=0) → TRAIL at -40."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=200.0,
        entry_ts=DEFAULT_BAR_END,
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=100.0)  # bar low 100 ≤ trail target → TRAIL
    inds = _inds(bars)
    sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TRAIL"
    # Bar-driven path fills at the exact trail target (peak 0 − 40 = −40),
    # not at the bar close — matching the live tick-driven fill.
    assert sig.payload["pnl_points"] == -40.0
    assert sig.price == 160.0  # entry 200 + trail_pnl(−40)


def test_no_di_jump_exit():
    """DI_JUMP_1M removed: -DI spike >10pts must NOT trigger any exit."""
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=DEFAULT_BAR_END,
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=110.0)  # pnl=+10, far from TP=50 + above trail
    inds = _inds(bars, minus_prev=15.0, minus_curr=27.0, plus_curr=28.0)
    sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(_event(bars, inds))
    assert sig is None
    assert st.position is not None  # still open


# ─── 8. cooldown ─────────────────────────────────────────────────────────


def test_cooldown_blocks_until_window_elapses():
    st = mod._state_for(TradeStrat1K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=DEFAULT_BAR_END,
        peak_pnl=0.0,
    )

    bars = _bars(5, last_close=50.0)
    inds = _inds(bars)
    exit_ev = _event(bars, inds)
    sig_exit = TradeStrat1K(params=TradeStrat1KParams()).on_bar(exit_ev)
    assert sig_exit is not None and sig_exit.side == "EXIT"
    expected_cooldown_until = exit_ev.bucket + timedelta(seconds=300)
    assert st.cooldown_until == expected_cooldown_until
    assert st.position is None

    base_bucket = exit_ev.bucket
    bars_e = _bars(5, last_close=200.0)
    inds_e = _inds(bars_e)
    for offset_seconds in (60, 120, 180, 240, 299):
        ev = _event(
            bars_e, inds_e,
            bucket=base_bucket + timedelta(seconds=offset_seconds),
        )
        sig = TradeStrat1K(params=TradeStrat1KParams()).on_bar(ev)
        assert sig is None
        assert st.cooldown_until == expected_cooldown_until
        assert st.position is None
        assert st.last_long_ready is False

    # Slip a non-firing event past cooldown so cooldown clears + latch
    # re-arms cleanly without firing on the same bar.
    release_bucket = base_bucket + timedelta(seconds=301)
    bars_low = _bars(5, last_close=200.0)
    # Block one gate (KD floor exceeded).
    inds_low = _inds(bars_low, k_prev=70.0)
    sig_low = TradeStrat1K(params=TradeStrat1KParams()).on_bar(
        _event(bars_low, inds_low, bucket=release_bucket)
    )
    assert sig_low is None
    assert st.cooldown_until is None
    assert st.last_long_ready is False

    fire_bucket = base_bucket + timedelta(seconds=360)
    sig_fire = TradeStrat1K(params=TradeStrat1KParams()).on_bar(
        _event(bars_e, inds_e, bucket=fire_bucket)
    )
    assert sig_fire is not None
    assert sig_fire.side == "LONG"


# ─── 9. on_tick path ─────────────────────────────────────────────────────


def test_on_tick_fires_entry_at_tick_ts():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    bucket = bars.index[-1].to_pydatetime()
    tick_ts = bucket + timedelta(seconds=17)
    ev = _tick_event(bars, inds, ts=tick_ts, price=205.0)

    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.side == "LONG"
    assert sig.ts == tick_ts
    assert sig.ts != bucket
    assert sig.price == 205.0
    assert sig.payload["fill_hint"] == "tick"


def test_on_tick_fires_tp_at_tick_price():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"
    entry_price = sig_open.price

    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=23)
    tick_price = entry_price + 50.0 + 1.0
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
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)

    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None
    bucket = bars.index[-1].to_pydatetime()
    exit_tick_ts = bucket + timedelta(seconds=5)
    ev_exit = _tick_event(
        bars, inds, ts=exit_tick_ts, price=sig_open.price + 51.0
    )
    sig_exit = strat.on_tick(ev_exit)
    assert sig_exit is not None and sig_exit.side == "EXIT"

    early_ts = exit_tick_ts + timedelta(seconds=10)
    ev_early = _tick_event(bars, inds, ts=early_ts, price=205.0)
    sig_early = strat.on_tick(ev_early)
    assert sig_early is None

    late_ts = exit_tick_ts + timedelta(seconds=301)
    ev_late = _tick_event(bars, inds, ts=late_ts, price=205.0)
    sig_late = strat.on_tick(ev_late)
    assert sig_late is not None
    assert sig_late.side == "LONG"
    assert sig_late.ts == late_ts


# ─── 10. Entry-window gate ───────────────────────────────────────────────


def test_entry_blocked_at_08_44_59():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((8, 44, 59))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_allowed_at_08_45_open_boundary():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((8, 45, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_allowed_at_09_10():
    """09:10 (old open boundary) — now well inside the new 08:45 window."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((9, 10, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None


def test_entry_allowed_at_13_44_59():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((13, 44, 59))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_blocked_at_13_45_close_boundary():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((13, 45, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_at_14_30():
    """[13:45, 15:00) closed gap."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((14, 30, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_allowed_at_15_00_night_open():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((15, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_allowed_at_22_00():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((22, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None


def test_entry_allowed_at_23_59_59():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((23, 59, 59))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None


def test_entry_allowed_at_00_30_overnight():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((0, 30, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None


def test_entry_allowed_at_04_59_59():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((4, 59, 59))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None


def test_entry_blocked_at_05_00_night_close():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((5, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_blocked_at_07_00_morning_gap():
    """[05:00, 08:45) closed gap."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((7, 0, 0))
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_exit_runs_outside_window():
    """Open at 13:00 Taipei (in window); tick at 14:30 (closed gap) →
    EOW force-close fires (the current source closes any open position the
    moment ``ts`` falls outside the entry window, so the position never
    rides through the closed gap)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((13, 0, 0))
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"

    tick_ts = _taipei_ts((14, 30, 0))
    ev = _tick_event(
        bars, inds, ts=tick_ts, price=sig_open.price + 40.0
    )
    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "EOW"


def test_exit_runs_inside_window_tp():
    """Open at 13:00 Taipei; a TP-crossing tick still inside the day window
    closes the position with a TP exit (TP=40 in the [10:31, 13:45) bucket)."""
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars_at_taipei((13, 0, 0))
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"

    tick_ts = _taipei_ts((13, 30, 0))
    ev = _tick_event(
        bars, inds, ts=tick_ts, price=sig_open.price + 40.0
    )
    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"


def test_window_reopen_fires_after_block():
    """13:44:59 (open) fires; 13:45:00 (closed) blocks + resets latch;
    15:00:00 (night reopens) fires."""
    strat = TradeStrat1K(params=TradeStrat1KParams())

    bars1 = _bars_at_taipei((13, 44, 59))
    inds1 = _inds(bars1)
    sig1 = strat.on_bar(_event(bars1, inds1))
    assert sig1 is not None and sig1.side == "LONG"

    st = _STATE[(TradeStrat1K.name, SYM)]
    st.position = None
    st.cooldown_until = None
    assert st.last_long_ready is True

    bars2 = _bars_at_taipei((13, 45, 0))
    inds2 = _inds(bars2)
    sig2 = strat.on_bar(_event(bars2, inds2))
    assert sig2 is None
    assert st.last_long_ready is False

    bars3 = _bars_at_taipei((15, 0, 0))
    inds3 = _inds(bars3)
    sig3 = strat.on_bar(_event(bars3, inds3))
    assert sig3 is not None
    assert sig3.side == "LONG"


# ─── 11. payload schema ──────────────────────────────────────────────────


def test_open_payload_drops_legacy_keys():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    payload = sig.payload
    assert "tp_points" in payload
    assert "trail_points" in payload
    assert "sl_points" not in payload
    assert "di_jump_points" not in payload


def test_exit_payload_carries_tod_resolved_pnl():
    strat = TradeStrat1K(params=TradeStrat1KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None

    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=10)
    ev = _tick_event(bars, inds, ts=tick_ts, price=sig_open.price + 50.0)
    sig = strat.on_tick(ev)
    assert sig is not None and sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"
    assert sig.payload["pnl_points"] == 50.0
    assert sig.payload["entry_price"] == sig_open.price
    assert sig.payload["entry_side"] == "LONG"
    assert sig.payload["fill_hint"] == "tick"
