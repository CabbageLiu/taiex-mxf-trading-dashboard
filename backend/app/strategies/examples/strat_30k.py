"""TAIEX 30-minute Strategy (30K策略).

Single-resolution LONG-only strategy. Entry fires the moment the four
30-minute gates align AND the 5-minute MACD histogram is positive AND
the timestamp lies inside the entry trading window (tick-driven); exits
(TP / SL / TRAIL) fire the moment the tick price crosses the threshold.
Both ``on_bar`` (back-compat / backtest path) and ``on_tick`` route
through the same ``_evaluate`` helper.

Entry gates (all must hold; LONG only):
  0. ``ts`` ∈ [09:10, 12:15) ∪ [15:00, 24:00) Asia/Taipei (half-open).
  1. ``price > MA120[-1]`` AND MA120 rising (``ma[-1] > ma[-2]``).
  2. KD: ``k[-2] > d[-2]`` AND ``k[-1] > d[-1]`` AND ``k[-2] < 75``.
  3. MACD histogram (30m): ``hist[-2] < 0`` AND ``hist[-1] > 0``.
  4. DMI (30m): ``plus[-2] > minus[-2]`` AND ``plus[-1] > minus[-1]``
     AND ``minus[-1] < minus[-2]``.
  5. 5-minute MACD histogram > 0 (auxiliary cross-resolution gate).
     Read from ``ev.indicators["macd_5m"]`` populated by the framework
     via ``aux_indicator_specs``. Stale data (last 5m bar older than
     15 minutes vs ``ts``) treated as "missing" → block.

Exit priority (per tick or bar close, first match wins):
  TP    — pnl ≥ 180
  SL    — pnl ≤ −70
  TRAIL — pnl ≤ peak_pnl − 80 (peak tracked from entry, starts at 0)

Cooldown: 9000 seconds (5 × 30m) after EXIT (time-based, not
bar-counted). While ``ts < cooldown_until`` evaluation returns None and
resets ``last_long_ready`` so the rising-edge latch re-arms once
cooldown clears.

Fill convention: tick-driven (not bar close). ``Signal.ts`` carries the
raw tick timestamp, so ``signals.ts`` / ``trades.entry_ts`` /
``trades.exit_ts`` reflect actual fill time.

Strategy instance is rebuilt per dispatch; state lives in module-level
``_STATE: dict[(name, symbol), _StratState]``. The strict ``_STATE``
naming is required by the backtest engine's snapshot/restore
introspection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel, Field

from app.config import get_settings
from app.strategies.base import BarEvent, Signal, Strategy, TickEvent, in_entry_window
from app.strategies.registry import register_strategy

_DAY_OPEN = time(9, 10)
_DAY_CLOSE = time(12, 15)
_NIGHT_OPEN = time(15, 0)


class TradeStrat30KParams(BaseModel):
    enable_short: bool = False

    kd_period: int = 9
    kd_k_smooth: int = 3
    kd_d_smooth: int = 3
    kd_long_floor: float = 75.0  # the `< 75` ceiling on the first KS

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    dmi_period: int = 14

    tp_points: float = 180.0
    sl_points: float = 70.0
    trail_points: float = 80.0

    cooldown_seconds: int = Field(default=9000, ge=0)


@dataclass
class _PositionState:
    side: str  # always "LONG" given enable_short=False default
    entry_price: float
    entry_ts: datetime
    entry_ind: dict[str, float | None] = field(default_factory=dict)
    peak_pnl: float = 0.0


@dataclass
class _StratState:
    position: _PositionState | None = None
    cooldown_until: datetime | None = None
    last_long_ready: bool = False


_STATE: dict[tuple[str, str], _StratState] = {}


def _state_for(name: str, symbol: str) -> _StratState:
    key = (name, symbol)
    st = _STATE.get(key)
    if st is None:
        st = _StratState()
        _STATE[key] = st
    return st


def _scalar(series: pd.Series, idx: int = -1) -> float | None:
    if series is None or len(series) + idx < 0:
        return None
    val = series.iloc[idx]
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if not math.isnan(f) else None


def _snapshot_ind(
    kd: pd.DataFrame | None,
    macd: pd.DataFrame | None,
    dmi: pd.DataFrame | None,
) -> dict[str, float | None]:
    """Snapshot the latest KD / MACD / DMI scalars into a fixed-shape dict.

    Returns a dict that *always* contains all 8 keys (k, d, macd, signal,
    hist, plus_di, minus_di, adx). Missing / NaN values are emitted as
    ``None``. Numeric values are rounded to 2 decimals. MA is intentionally
    not part of the snapshot — schema parity with v1/v2.
    """
    keys = ("k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx")
    snap: dict[str, float | None] = dict.fromkeys(keys)

    def _read(df: pd.DataFrame | None, col: str) -> float | None:
        if df is None or col not in df.columns:
            return None
        try:
            val = df[col].iloc[-1]
        except (IndexError, KeyError):
            return None
        if val is None:
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        if math.isnan(f):
            return None
        return round(f, 2)

    snap["k"] = _read(kd, "k")
    snap["d"] = _read(kd, "d")
    snap["macd"] = _read(macd, "macd")
    snap["signal"] = _read(macd, "signal")
    snap["hist"] = _read(macd, "hist")
    snap["plus_di"] = _read(dmi, "plus_di")
    snap["minus_di"] = _read(dmi, "minus_di")
    snap["adx"] = _read(dmi, "adx")
    return snap


def _long_entry_now(
    close_curr: float | None,
    ma_prev: float | None,
    ma_curr: float | None,
    k_prev: float | None,
    d_prev: float | None,
    k_curr: float | None,
    d_curr: float | None,
    hist_prev: float | None,
    hist_curr: float | None,
    plus_prev: float | None,
    plus_curr: float | None,
    minus_prev: float | None,
    minus_curr: float | None,
    kd_long_floor: float,
) -> bool:
    """Evaluate the four primary-resolution LONG entry gates.

    The 5m MACD gate + entry-window gate are evaluated separately by the
    caller; this helper covers only the closed-bar primary gates so it
    can be reused/tested in isolation.
    """
    if None in (
        close_curr, ma_prev, ma_curr,
        k_prev, d_prev, k_curr, d_curr,
        hist_prev, hist_curr,
        plus_prev, plus_curr, minus_prev, minus_curr,
    ):
        return False
    # 1. close above MA, MA rising
    gate_ma = close_curr > ma_curr and ma_curr > ma_prev
    # 2. KD: two consecutive K>D, first K below floor
    gate_kd = k_prev > d_prev and k_curr > d_curr and k_prev < kd_long_floor
    # 3. MACD histogram crosses zero from below
    gate_macd = hist_prev < 0 and hist_curr > 0
    # 4. DMI: two consecutive +DI>-DI, second -DI shrinks
    gate_dmi = (
        plus_prev > minus_prev
        and plus_curr > minus_curr
        and minus_curr < minus_prev
    )
    return gate_ma and gate_kd and gate_macd and gate_dmi


def _macd_5m_positive(
    macd_5m: pd.DataFrame | None,
    ts: datetime,
    *,
    max_age: timedelta = timedelta(minutes=15),
) -> bool:
    """Evaluate the 5-minute MACD histogram > 0 confirmation gate.

    Block when:
      - The DataFrame is missing or empty.
      - The most recent 5m bar is older than ``max_age`` vs ``ts``
        (FinMind outage / aux feed stalled).
      - The latest histogram value is None / NaN / ≤ 0.
    """
    if macd_5m is None or len(macd_5m) == 0:
        return False
    last_ts = macd_5m.index[-1]
    if hasattr(last_ts, "to_pydatetime"):
        last_ts = last_ts.to_pydatetime()
    # Compare TZ-aware to TZ-aware; if ts is TZ-naive vs aware index, convert.
    if last_ts.tzinfo is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=last_ts.tzinfo)
    elif last_ts.tzinfo is None and ts.tzinfo is not None:
        last_ts = last_ts.replace(tzinfo=ts.tzinfo)
    if (ts - last_ts) >= max_age:
        return False
    hist = _scalar(macd_5m["hist"], idx=-1)
    if hist is None:
        return False
    return hist > 0


@register_strategy
class TradeStrat30K(Strategy):
    name: ClassVar[str] = "strat_30k"
    display_name: ClassVar[str] = "30K策略"
    description: ClassVar[str] = (
        "30 分鐘多單策略；進場：close>MA120 且 MA120 向上、KD 連兩 KS>DS 且第一根 KS<75、"
        "30m MACD 直方翻正、+DI>-DI 且 -DI 縮、5m MACD>0 且時段在 09:10-12:15 / 15:00-24:00；"
        "出場：TP 180 / SL −70 / 移動停損 80。"
    )
    spec: ClassVar[dict[str, str]] = {
        "週期": "30 分鐘 (5 分鐘輔助)",
        "進場時段": "09:10-12:15 與 15:00-24:00 (Asia/Taipei；半開區間)",
        "進場": (
            "close>MA120 且 MA120 向上；KD 連兩根 KS>DS 且第一根 KS<75；"
            "30 分鐘 MACD 直方圖由負翻正；+DI>-DI 連兩根且第二根 -DI 縮；"
            "5 分鐘 MACD 直方圖>0"
        ),
        "出場": "獲利 180 點 / 虧損 70 點 / 移動停損 80 點",
        "冷卻": "出場後 9000 秒 (5 × 30 分鐘)",
        "備註": "僅多單；訊號逐筆即時觸發",
    }
    resolutions: ClassVar[list[str]] = ["30m"]
    tick_resolutions: ClassVar[list[str]] = ["30m"]
    params_schema: ClassVar[type[BaseModel]] = TradeStrat30KParams
    indicator_specs: ClassVar[dict[str, dict]] = {
        "ma120": {"kind": "ma", "params": {"period": 120, "kind": "sma"}},
        "kd": {"kind": "kd", "params": {"period": 9, "k_smooth": 3, "d_smooth": 3}},
        "macd": {"kind": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
        "dmi": {"kind": "dmi", "params": {"period": 14}},
    }
    aux_indicator_specs: ClassVar[dict[str, dict]] = {
        "macd_5m": {
            "kind": "macd",
            "params": {"fast": 12, "slow": 26, "signal": 9},
            "resolution": "5m",
        },
    }

    @classmethod
    def dump_state(cls, symbol: str) -> dict:
        st = _STATE.get((cls.name, symbol))
        if st is None:
            return {}
        pos = st.position
        return {
            "cooldown_until": (
                st.cooldown_until.isoformat() if st.cooldown_until else None
            ),
            "last_long_ready": st.last_long_ready,
            "position": (
                {
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "entry_ts": pos.entry_ts.isoformat(),
                    "peak_pnl": pos.peak_pnl,
                }
                if pos
                else None
            ),
        }

    def on_bar(self, ev: BarEvent) -> Signal | None:
        st = _state_for(self.name, ev.symbol)
        close = _scalar(ev.bars["close"])
        if close is None:
            return None
        bar_high = _scalar(ev.bars["high"])
        bar_low = _scalar(ev.bars["low"])
        return self._evaluate(
            ev.bucket, close, ev.bars, ev.indicators, st, self.params,
            symbol=ev.symbol, resolution=ev.resolution,
            bar_high=bar_high, bar_low=bar_low,
        )

    def on_tick(self, ev: TickEvent) -> Signal | None:
        st = _state_for(self.name, ev.symbol)
        return self._evaluate(
            ev.ts, ev.price, ev.bars, ev.indicators, st, self.params,
            symbol=ev.symbol, resolution=ev.resolution,
            bar_high=None, bar_low=None,
        )

    def _evaluate(
        self,
        ts: datetime,
        price: float,
        bars: pd.DataFrame,
        indicators: dict[str, pd.DataFrame],
        st: _StratState,
        p: TradeStrat30KParams,
        *,
        symbol: str,
        resolution: str,
        bar_high: float | None = None,
        bar_low: float | None = None,
    ) -> Signal | None:
        # 0. End-of-window force-close. Direct ts-vs-window check (not edge
        # detect) so the EOW exit fires even after a missed-tick gap or
        # state rehydration. `st.position = None` after emit is the natural
        # idempotency guard — subsequent ticks won't re-enter this branch.
        pos = st.position
        if pos is not None and not in_entry_window(
            ts,
            get_settings().tz,
            day_open=_DAY_OPEN,
            day_close=_DAY_CLOSE,
            night_open=_NIGHT_OPEN,
        ):
            exit_price = price
            pnl_points = (
                (exit_price - pos.entry_price)
                if pos.side == "LONG"
                else (pos.entry_price - exit_price)
            )
            kd = indicators.get("kd")
            macd = indicators.get("macd")
            dmi = indicators.get("dmi")
            # EOW honest snapshot — do NOT fall back to entry_ind even when all
            # current indicators are None. Stamping entry_ind as exit_ind
            # would poison post-trade analysis with stale data.
            exit_ind_snapshot = _snapshot_ind(kd, macd, dmi)
            st.position = None
            st.cooldown_until = ts + timedelta(seconds=p.cooldown_seconds)
            st.last_long_ready = False
            return Signal(
                ts=ts,
                symbol=symbol,
                resolution=resolution,
                strategy=self.name,
                side="EXIT",
                price=exit_price,
                reason="EOW",
                payload={
                    "exit_reason": "EOW",
                    "exit_ind": exit_ind_snapshot,
                    "entry_price": pos.entry_price,
                    "entry_side": pos.side,
                    "pnl_points": round(pnl_points, 2),
                    "fill_hint": "tick",
                },
            )

        # 1. Cooldown gate. While inside the window, suppress and re-arm latch
        # so the first tick after release re-evaluates as a rising edge.
        if st.cooldown_until is not None:
            if ts < st.cooldown_until:
                st.last_long_ready = False
                return None
            st.cooldown_until = None

        # 2. Manage open position. Exits run regardless of trading window /
        # 5m MACD — open positions must always be closeable.
        if st.position is not None:
            return self._manage_open_position(
                ts, price, indicators, st, p,
                symbol=symbol, resolution=resolution,
                bar_high=bar_high, bar_low=bar_low,
            )

        # 3. Maybe enter.
        return self._maybe_enter(
            ts, price, bars, indicators, st, p,
            symbol=symbol, resolution=resolution,
        )

    def _manage_open_position(
        self,
        ts: datetime,
        price: float,
        indicators: dict[str, pd.DataFrame],
        st: _StratState,
        p: TradeStrat30KParams,
        *,
        symbol: str,
        resolution: str,
        bar_high: float | None = None,
        bar_low: float | None = None,
    ) -> Signal | None:
        pos = st.position
        if pos is None:
            return None

        kd = indicators.get("kd")
        macd = indicators.get("macd")
        dmi = indicators.get("dmi")
        snapshot = _snapshot_ind(kd, macd, dmi)
        if all(v is None for v in snapshot.values()) and pos.entry_ind:
            snapshot = dict(pos.entry_ind)

        # Bar-driven backtest path — fill at exact SL / TP / TRAIL target
        # if the bar's extremes crossed it. Pessimistic SL → TP → TRAIL
        # ordering matches Pine convention.
        if bar_high is not None and bar_low is not None:
            if pos.side == "LONG":
                sl_target = pos.entry_price - p.sl_points
                if bar_low <= sl_target:
                    return self._close_position(
                        ts, sl_target, "SL", -p.sl_points,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )
                tp_target = pos.entry_price + p.tp_points
                if bar_high >= tp_target:
                    return self._close_position(
                        ts, tp_target, "TP", p.tp_points,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )
                effective_peak = max(pos.peak_pnl, bar_high - pos.entry_price)
                trail_pnl = effective_peak - p.trail_points
                trail_target = pos.entry_price + trail_pnl
                if bar_low <= trail_target:
                    pos.peak_pnl = effective_peak
                    return self._close_position(
                        ts, trail_target, "TRAIL", trail_pnl,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )
                pos.peak_pnl = effective_peak
            else:  # SHORT mirror.
                sl_target = pos.entry_price + p.sl_points
                if bar_high >= sl_target:
                    return self._close_position(
                        ts, sl_target, "SL", -p.sl_points,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )
                tp_target = pos.entry_price - p.tp_points
                if bar_low <= tp_target:
                    return self._close_position(
                        ts, tp_target, "TP", p.tp_points,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )
                effective_peak = max(pos.peak_pnl, pos.entry_price - bar_low)
                trail_pnl = effective_peak - p.trail_points
                trail_target = pos.entry_price - trail_pnl
                if bar_high >= trail_target:
                    pos.peak_pnl = effective_peak
                    return self._close_position(
                        ts, trail_target, "TRAIL", trail_pnl,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )
                pos.peak_pnl = effective_peak
            return None

        if pos.side == "LONG":
            pnl = price - pos.entry_price
        else:
            pnl = pos.entry_price - price

        # Priority order: TP → SL → TRAIL. (No DI_JUMP exit on 30K.)
        if pnl >= p.tp_points:
            return self._close_position(
                ts, price, "TP", pnl,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )
        if pnl <= -p.sl_points:
            return self._close_position(
                ts, price, "SL", pnl,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )
        if pnl <= pos.peak_pnl - p.trail_points:
            return self._close_position(
                ts, price, "TRAIL", pnl,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )

        pos.peak_pnl = max(pos.peak_pnl, pnl)
        return None

    def _maybe_enter(
        self,
        ts: datetime,
        price: float,
        bars: pd.DataFrame,
        indicators: dict[str, pd.DataFrame],
        st: _StratState,
        p: TradeStrat30KParams,
        *,
        symbol: str,
        resolution: str,
    ) -> Signal | None:
        # Window gate first: outside the trading window, suppress and reset
        # latch so a window-reopen with pre-aligned gates fires cleanly as
        # a fresh rising edge on the first tick after reopen.
        if not in_entry_window(
            ts,
            get_settings().tz,
            day_open=_DAY_OPEN,
            day_close=_DAY_CLOSE,
            night_open=_NIGHT_OPEN,
        ):
            st.last_long_ready = False
            return None

        # 5m MACD confirmation gate (auxiliary cross-resolution).
        macd_5m = indicators.get("macd_5m")
        if not _macd_5m_positive(macd_5m, ts):
            st.last_long_ready = False
            return None

        ma = indicators.get("ma120")
        kd = indicators.get("kd")
        macd = indicators.get("macd")
        dmi = indicators.get("dmi")
        if ma is None or kd is None or macd is None or dmi is None:
            return None

        ma_prev = _scalar(ma["ma"], idx=-2)
        ma_curr = _scalar(ma["ma"], idx=-1)
        k_prev = _scalar(kd["k"], idx=-2)
        d_prev = _scalar(kd["d"], idx=-2)
        k_curr = _scalar(kd["k"], idx=-1)
        d_curr = _scalar(kd["d"], idx=-1)
        hist_prev = _scalar(macd["hist"], idx=-2)
        hist_curr = _scalar(macd["hist"], idx=-1)
        plus_prev = _scalar(dmi["plus_di"], idx=-2)
        plus_curr = _scalar(dmi["plus_di"], idx=-1)
        minus_prev = _scalar(dmi["minus_di"], idx=-2)
        minus_curr = _scalar(dmi["minus_di"], idx=-1)

        # Pass tick `price` as `close_curr` so close>MA gate evaluates against
        # live tick price (intra-bar firing).
        long_now = _long_entry_now(
            price, ma_prev, ma_curr,
            k_prev, d_prev, k_curr, d_curr,
            hist_prev, hist_curr,
            plus_prev, plus_curr, minus_prev, minus_curr,
            kd_long_floor=p.kd_long_floor,
        )

        long_rising = long_now and not st.last_long_ready
        st.last_long_ready = long_now

        if not long_rising:
            return None

        return self._open_position(
            ts, price, side="LONG",
            st=st, p=p, kd=kd, macd=macd, dmi=dmi,
            symbol=symbol, resolution=resolution,
        )

    def _open_position(
        self,
        ts: datetime,
        price: float,
        *,
        side: str,
        st: _StratState,
        p: TradeStrat30KParams,
        kd: pd.DataFrame,
        macd: pd.DataFrame,
        dmi: pd.DataFrame,
        symbol: str,
        resolution: str,
    ) -> Signal:
        snap = _snapshot_ind(kd, macd, dmi)
        st.position = _PositionState(
            side=side, entry_price=price, entry_ts=ts,
            entry_ind=dict(snap), peak_pnl=0.0,
        )
        nan = float("nan")
        k_disp = snap.get("k") if snap.get("k") is not None else nan
        d_disp = snap.get("d") if snap.get("d") is not None else nan
        macd_disp = snap.get("macd") if snap.get("macd") is not None else nan
        plus_disp = snap.get("plus_di") if snap.get("plus_di") is not None else nan
        return Signal(
            ts=ts,
            symbol=symbol,
            resolution=resolution,
            strategy=self.name,
            side=side,
            price=price,
            reason=(
                f"entry {side}: K={k_disp:.1f} D={d_disp:.1f} "
                f"MACD={macd_disp:.2f} +DI={plus_disp:.1f}"
            ),
            payload={
                "entry_ind": snap,
                "tp_points": p.tp_points,
                "sl_points": p.sl_points,
                "trail_points": p.trail_points,
                "fill_hint": "tick",
            },
        )

    def _close_position(
        self,
        ts: datetime,
        price: float,
        reason: str,
        pnl: float,
        *,
        st: _StratState,
        p: TradeStrat30KParams,
        exit_ind: dict[str, float | None] | None = None,
        symbol: str,
        resolution: str,
    ) -> Signal:
        pos = st.position
        st.position = None
        st.cooldown_until = ts + timedelta(seconds=p.cooldown_seconds)
        st.last_long_ready = False
        if exit_ind is None:
            exit_ind = _snapshot_ind(None, None, None)
        return Signal(
            ts=ts,
            symbol=symbol,
            resolution=resolution,
            strategy=self.name,
            side="EXIT",
            price=price,
            reason=f"exit {reason}: pnl={pnl:+.1f} pts",
            payload={
                "exit_reason": reason,
                "pnl_points": round(pnl, 2),
                "entry_price": pos.entry_price if pos else None,
                "entry_side": pos.side if pos else None,
                "exit_ind": exit_ind,
                "fill_hint": "tick",
            },
        )
