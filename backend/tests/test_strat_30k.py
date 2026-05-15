from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import BarEvent, TickEvent
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
TPE = ZoneInfo("Asia/Taipei")


@pytest.fixture(autouse=True)
def reset_state():
    _STATE.clear()
    yield
    _STATE.clear()


def _tpe_dt(year: int, month: int, day: int, hour: int, minute: int = 0,
            second: int = 0, micro: int = 0) -> datetime:
    """Build a Taipei-local datetime in UTC. Strategy reads ``ts`` and
    converts via ``ts.astimezone(tz)`` — same instant in UTC works for
    the window check.
    """
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=TPE)


def _bars(
    n: int,
    *,
    last_close: float,
    slope: float = 0.5,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Synthesize OHLC bars with monotonically rising close.

    `slope` (>0) makes the close series rising so we can place `last_close`
    above MA[-1]. open=high=low=close keeps things simple — strategies only
    read close. The default `end` puts the last bar at a Taipei-local time
    inside the entry window so the window gate doesn't have to be worked
    around in every test.
    """
    if end is None:
        # 11:00 Taipei == 03:00 UTC on the same calendar date — inside the
        # day session entry window [09:15, 12:15) Taipei.
        end = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
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


def _macd_5m(
    *,
    bucket: datetime | None = None,
    hist: float = 1.0,
    n: int = 5,
    age_minutes: float = 0.0,
) -> pd.DataFrame:
    """Build a tiny 5m MACD DataFrame whose latest bar is `age_minutes`
    older than `bucket`. Default: latest bar at `bucket` itself (fresh).
    """
    if bucket is None:
        bucket = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
    last = bucket - timedelta(minutes=age_minutes)
    idx = pd.date_range(end=last, periods=n, freq="5min")
    return pd.DataFrame(
        {
            "macd": np.full(n, 0.5, dtype=float),
            "signal": np.full(n, 0.2, dtype=float),
            "hist": np.full(n, hist, dtype=float),
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
    macd_5m: pd.DataFrame | None | object = ...,  # type: ignore[assignment]
    macd_5m_hist: float = 1.0,
    macd_5m_age_minutes: float = 0.0,
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

    # macd_5m sentinel: ... (default) → build one anchored at bars.index[-1].
    # None → omit aux indicator (simulates missing). DataFrame → use as-is.
    if macd_5m is ...:
        bucket = bars.index[-1].to_pydatetime()
        out["macd_5m"] = _macd_5m(
            bucket=bucket,
            hist=macd_5m_hist,
            age_minutes=macd_5m_age_minutes,
        )
    elif macd_5m is None:
        # explicit omission — caller wants to test missing-aux blocking
        pass
    else:
        out["macd_5m"] = macd_5m  # type: ignore[assignment]
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
    assert sig.payload["fill_hint"] == "tick"


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


# ─── 4. KD floor (now 75) ────────────────────────────────────────────────


def test_no_entry_when_first_k_at_floor():
    """k_prev at the new 75 floor must NOT fire (strict `<`)."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=75.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_at_kd_boundary_below_floor():
    """k_prev just below 75 fires entry."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=74.99)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_no_entry_at_old_80_floor():
    """k_prev=78 used to fire under the old 80 floor; now must NOT fire."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, k_prev=78.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


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
    _STATE[(TradeStrat30K.name, SYM)] = _StratState(last_long_ready=True)
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None
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
    # Cooldown is now seconds-based (default 9000s); cooldown_until is anchored
    # at the exit timestamp.
    assert st.cooldown_until == bucket2 + timedelta(seconds=9000)
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


# ─── 11. cooldown (time-based, 9000s) ────────────────────────────────────


def test_cooldown_blocks_until_window_elapses():
    """Cooldown is seconds-based (9000s = 5 × 30m): blocks while ts <
    cooldown_until, then re-arms.

    Anchor at 21:00 Taipei (night window). Exit + ALL probe ticks
    fall in [21:00, 24:00) so the entry-window gate never interferes
    with the cooldown semantic under test.
    """
    p = TradeStrat30KParams(cooldown_seconds=9000)
    st = mod._state_for(TradeStrat30K.name, SYM)
    st.position = _PositionState(
        side="LONG", entry_price=100.0,
        entry_ts=_tpe_dt(2026, 5, 1, 20, 30),
        peak_pnl=0.0,
    )

    # Exit at 21:00 Taipei. pnl = -80 ≤ -sl=70 → SL.
    bars, _, exit_bucket = _make_event_at(_tpe_dt(2026, 5, 1, 21, 0), price=20.0)
    inds = _inds(bars)
    inds["macd_5m"] = _macd_5m(bucket=exit_bucket, hist=1.0)
    exit_ev = _event(bars, inds, bucket=exit_bucket)
    sig_exit = TradeStrat30K(params=p).on_bar(exit_ev)
    assert sig_exit is not None and sig_exit.side == "EXIT"
    expected_cooldown_until = exit_bucket + timedelta(seconds=9000)
    assert st.cooldown_until == expected_cooldown_until
    assert st.position is None

    # Within the cooldown window: each bar suppressed; latch stays False.
    # Probe offsets all fall in [21:00, 23:30) Taipei = night window.
    bars_e, inds_e, _ = _make_event_at(_tpe_dt(2026, 5, 1, 22, 0), price=200.0)
    for offset_seconds in (60, 1800, 4500, 7200, 8999):
        probe_bucket = exit_bucket + timedelta(seconds=offset_seconds)
        # Re-anchor 5m aux at probe time so staleness is fine.
        inds_probe = dict(inds_e)
        inds_probe["macd_5m"] = _macd_5m(bucket=probe_bucket, hist=1.0)
        ev = _event(bars_e, inds_probe, bucket=probe_bucket)
        sig = TradeStrat30K(params=p).on_bar(ev)
        assert sig is None
        assert st.cooldown_until == expected_cooldown_until
        assert st.position is None
        assert st.last_long_ready is False

    # Slip a non-firing event past the cooldown so cooldown_until clears + latch
    # re-arms cleanly without firing on the same bar (gates fail this round).
    release_bucket = exit_bucket + timedelta(seconds=9001)
    bars_low = _bars(5, last_close=200.0, end=release_bucket.astimezone(UTC))
    inds_low = _inds(bars_low, hist_prev=0.5)
    inds_low["macd_5m"] = _macd_5m(bucket=release_bucket, hist=1.0)
    sig_low = TradeStrat30K(params=p).on_bar(
        _event(bars_low, inds_low, bucket=release_bucket)
    )
    assert sig_low is None
    assert st.cooldown_until is None
    assert st.last_long_ready is False

    # Next aligned bar after cooldown clears → entry fires.
    fire_bucket = exit_bucket + timedelta(seconds=9300)
    bars_f = _bars(5, last_close=200.0, end=fire_bucket.astimezone(UTC))
    inds_f = _inds(bars_f)
    inds_f["macd_5m"] = _macd_5m(bucket=fire_bucket, hist=1.0)
    sig_fire = TradeStrat30K(params=p).on_bar(
        _event(bars_f, inds_f, bucket=fire_bucket)
    )
    assert sig_fire is not None
    assert sig_fire.side == "LONG"


# ─── 12. on_tick path ────────────────────────────────────────────────────


def test_on_tick_fires_entry_at_tick_ts():
    """on_tick fires LONG when gates align; Signal.ts == raw tick ts (mid-bucket)."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
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
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"
    entry_price = sig_open.price

    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=23)
    tick_price = entry_price + 180.0 + 1.0  # tp_points = 180
    ev = _tick_event(bars, inds, ts=tick_ts, price=tick_price)
    sig = strat.on_tick(ev)

    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"
    assert sig.ts == tick_ts
    assert sig.payload["fill_hint"] == "tick"

    st = _STATE[(TradeStrat30K.name, SYM)]
    assert st.position is None
    assert st.cooldown_until == tick_ts + timedelta(seconds=9000)


def test_on_tick_cooldown_blocks_then_releases():
    """After exit at T: tick during cooldown blocked; tick after cooldown
    with gates aligned fires.

    Anchor at 21:00 Taipei (night window). All ticks fall in
    [21:00, 24:00) so window gate never interferes.
    """
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 21, 0), price=200.0)

    # Open + immediate TP exit at bucket close.
    sig_open = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig_open is not None
    exit_tick_ts = bucket + timedelta(seconds=5)
    ev_exit = _tick_event(
        bars, inds, ts=exit_tick_ts, price=sig_open.price + 181.0
    )
    sig_exit = strat.on_tick(ev_exit)
    assert sig_exit is not None and sig_exit.side == "EXIT"

    # Tick during cooldown window → suppressed. Refresh 5m aux so staleness
    # is not what blocks (we want to assert the cooldown is doing the work).
    early_ts = exit_tick_ts + timedelta(seconds=600)
    inds_early = dict(inds)
    inds_early["macd_5m"] = _macd_5m(bucket=early_ts, hist=1.0)
    ev_early = _tick_event(bars, inds_early, ts=early_ts, price=205.0)
    sig_early = strat.on_tick(ev_early)
    assert sig_early is None

    # Tick after cooldown clears with gates aligned → entry fires.
    late_ts = exit_tick_ts + timedelta(seconds=9001)
    inds_late = dict(inds)
    inds_late["macd_5m"] = _macd_5m(bucket=late_ts, hist=1.0)
    ev_late = _tick_event(bars, inds_late, ts=late_ts, price=205.0)
    sig_late = strat.on_tick(ev_late)
    assert sig_late is not None
    assert sig_late.side == "LONG"
    assert sig_late.ts == late_ts


# ─── 13. 5m MACD confirmation gate ──────────────────────────────────────


def test_no_entry_when_macd_5m_missing():
    """No `macd_5m` aux indicator at all → entry blocked."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m=None)
    assert "macd_5m" not in inds
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None
    st = _STATE[(TradeStrat30K.name, SYM)]
    # The 5m-block path resets latch (matches cooldown / window semantics so
    # a fresh-aligned aux next bucket fires cleanly as a rising edge).
    assert st.last_long_ready is False


def test_no_entry_when_macd_5m_empty():
    """Empty 5m MACD DataFrame → entry blocked."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m=pd.DataFrame())
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_macd_5m_negative():
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m_hist=-0.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_macd_5m_zero():
    """Strict `> 0`: hist=0 is blocked."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m_hist=0.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_no_entry_when_macd_5m_stale():
    """5m MACD last bar > 15 min before ts → block."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m_hist=2.0, macd_5m_age_minutes=20.0)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is None


def test_entry_when_macd_5m_positive():
    """5m hist > 0 + primary gates aligned → entry fires."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars, macd_5m_hist=2.5)
    sig = strat.on_bar(_event(bars, inds))
    assert sig is not None
    assert sig.side == "LONG"


def test_exit_ignores_5m_macd_gate():
    """Open position + 5m MACD turns negative → TP/SL/TRAIL still fire.
    The 5m gate is entry-only.
    """
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None and sig_open.side == "LONG"
    entry_price = sig_open.price

    # Tick @ TP threshold but with 5m MACD negative — exit should still fire.
    tick_ts = bars.index[-1].to_pydatetime() + timedelta(seconds=30)
    bucket = bars.index[-1].to_pydatetime()
    inds_neg = dict(inds)
    inds_neg["macd_5m"] = _macd_5m(bucket=bucket, hist=-2.0)
    ev = _tick_event(
        bars, inds_neg, ts=tick_ts, price=entry_price + 181.0
    )
    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"


# ─── 14. entry-window gate ──────────────────────────────────────────────


def _make_event_at(local_ts: datetime, *, price: float = 200.0):
    """Build a primed `BarEvent` whose `bucket` == `local_ts` Taipei.

    All gates are aligned by default; 5m MACD is anchored fresh at
    `local_ts` so it never blocks.
    """
    # Convert to UTC to use as `end` for `_bars`, since pd.date_range with TZ
    # works fine but the strategy compares ts.astimezone(tz) — same instant.
    utc_end = local_ts.astimezone(UTC)
    bars = _bars(5, last_close=price, end=utc_end)
    bucket = bars.index[-1].to_pydatetime()
    inds = _inds(bars)
    inds["macd_5m"] = _macd_5m(bucket=bucket, hist=1.0)
    return bars, inds, bucket


def test_no_entry_outside_window_midday():
    """13:00 Taipei (inside 12:15–15:00 closed gap) → entry blocked."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 13, 0))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is None


def test_no_entry_at_14_59_59_boundary():
    """14:59:59 Taipei → blocked (one second before night-window open)."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 14, 59, 59, 999000))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is None


def test_no_entry_at_09_09_59_boundary():
    """09:09:59 Taipei → blocked (one second before day-window open)."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 9, 9, 59, 999000))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is None


def test_no_entry_outside_window_overnight():
    """03:00 Taipei (post-midnight) → entry blocked."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 3, 0))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is None


def test_no_entry_at_window_close_boundary():
    """12:15:00.000 Taipei → blocked (half-open: [09:15, 12:15))."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 12, 15, 0, 0))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is None


def test_entry_just_before_window_close():
    """12:14:59.999 Taipei → allowed."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 12, 14, 59, 999000))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_at_day_window_open_boundary():
    """09:10:00 Taipei → allowed (closed left edge: [09:10,...))."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 9, 10, 0, 0))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_at_night_window_open_boundary():
    """15:00:00 Taipei → allowed (closed left edge of night entry window)."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 15, 0, 0, 0))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_inside_day_window():
    """11:00 Taipei → allowed."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 11, 0))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is not None
    assert sig.side == "LONG"


def test_entry_inside_night_window():
    """22:00 Taipei → allowed."""
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 22, 0))
    sig = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig is not None
    assert sig.side == "LONG"


def test_exit_runs_at_13_00_taipei():
    """Window gate is entry-only; exits run anytime.

    Open at 11:00 Taipei → tick at 13:00 Taipei (outside window) crosses TP.
    """
    strat = TradeStrat30K(params=TradeStrat30KParams())
    bars, inds, bucket = _make_event_at(_tpe_dt(2026, 5, 1, 11, 0))
    sig_open = strat.on_bar(_event(bars, inds, bucket=bucket))
    assert sig_open is not None and sig_open.side == "LONG"
    entry_price = sig_open.price

    # Tick at 13:00 Taipei (outside window). Use same bars/inds; only ts changes.
    tick_ts = _tpe_dt(2026, 5, 1, 13, 0)
    ev = _tick_event(
        bars, inds, ts=tick_ts, price=entry_price + 181.0
    )
    sig = strat.on_tick(ev)
    assert sig is not None
    assert sig.side == "EXIT"
    assert sig.payload["exit_reason"] == "TP"


def test_window_reopen_rising_edge_redetect():
    """Gates align before 12:15; latch reset across the closed window;
    gates STILL aligned at 15:00 reopen → entry fires immediately on
    first tick after 15:00 (rising-edge re-detection).
    """
    p = TradeStrat30KParams()

    # Step 1: pre-set latch = True manually as if previous tick fired ready.
    st = mod._state_for(TradeStrat30K.name, SYM)
    st.last_long_ready = True

    # Step 2: tick at 13:00 (outside window) → window gate blocks + resets
    # latch.
    bars, inds, _ = _make_event_at(_tpe_dt(2026, 5, 1, 13, 0))
    mid_ts = _tpe_dt(2026, 5, 1, 13, 0)
    sig_mid = TradeStrat30K(params=p).on_tick(
        _tick_event(bars, inds, ts=mid_ts, price=200.0)
    )
    assert sig_mid is None
    assert st.last_long_ready is False

    # Step 3: tick at 15:00 (window reopens) with gates still aligned → fires.
    bars2, inds2, _ = _make_event_at(_tpe_dt(2026, 5, 1, 15, 0))
    reopen_ts = _tpe_dt(2026, 5, 1, 15, 0)
    sig_open = TradeStrat30K(params=p).on_tick(
        _tick_event(bars2, inds2, ts=reopen_ts, price=200.0)
    )
    assert sig_open is not None
    assert sig_open.side == "LONG"
    assert sig_open.ts == reopen_ts


# ─── 15. dump_state surface ─────────────────────────────────────────────


def test_dump_state_exposes_cooldown_until():
    """After a SL exit, dump_state reports `cooldown_until` ISO string."""
    p = TradeStrat30KParams()
    strat = TradeStrat30K(params=p)
    bars = _bars(5, last_close=200.0)
    inds = _inds(bars)
    sig_open = strat.on_bar(_event(bars, inds))
    assert sig_open is not None

    bars2 = _bars(5, last_close=200.0 - 70.0)
    bucket2 = bars.index[-1].to_pydatetime() + pd.Timedelta(minutes=30)
    sig_exit = TradeStrat30K(params=p).on_bar(
        _event(bars2, _inds(bars2), bucket=bucket2)
    )
    assert sig_exit is not None and sig_exit.side == "EXIT"

    snap = TradeStrat30K.dump_state(SYM)
    assert "cooldown_until" in snap
    assert snap["cooldown_until"] is not None
    assert isinstance(snap["cooldown_until"], str)
    # No `cooldown_left` field anymore.
    assert "cooldown_left" not in snap
    # Position should be cleared.
    assert snap["position"] is None
