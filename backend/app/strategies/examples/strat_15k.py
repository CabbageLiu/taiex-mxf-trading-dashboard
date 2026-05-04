"""TAIEX 15-minute Strategy (15K策略).

Single-resolution LONG-only strategy. Entry fires the moment the four 15m
gates align intra-bar (tick-driven), provided the entry-window gate and the
5-minute MACD-histogram-positive gate also pass. Exits (TP / SL / TRAIL) fire
the moment the tick price crosses the threshold; exits ignore the window
gate and the 5m MACD gate so an open position is always closeable. Both
``on_bar`` (back-compat / backtest path) and ``on_tick`` route through the
same ``_evaluate`` helper.

Entry gates (all four 15m gates AND window AND 5m MACD must hold; LONG only):
  Window — Asia/Taipei local time in [09:15, 12:15) ∪ [15:00, 24:00).
  5m MACD — auxiliary 5m MACD histogram > 0 at the latest closed 5m bar
            (cold/empty/stale-by-more-than-15min all block).
  1. ``price > MA120[-1]`` AND MA120 rising (``ma[-1] > ma[-2]``).
  2. KD: ``k[-2] > d[-2]`` AND ``k[-1] > d[-1]`` AND ``k[-2] < 75``.
  3. MACD histogram: ``hist[-2] < 0`` AND ``hist[-1] > 0``.
  4. DMI: ``plus[-2] > minus[-2]`` AND ``plus[-1] > minus[-1]`` AND
     ``minus[-1] < minus[-2]``.

Exit priority (per tick or bar close, first match wins):
  TP    — pnl ≥ 130
  SL    — pnl ≤ −70
  TRAIL — pnl ≤ peak_pnl − 80 (peak tracked from entry, starts at 0)

Cooldown: 4500 seconds (5 × 15m) after EXIT (time-based, not bar-counted).
While ``ts < cooldown_until`` evaluation returns None and resets
``last_long_ready`` so the rising-edge latch re-arms once cooldown clears.

Fill convention: tick-driven (not bar close). ``Signal.ts`` carries the
raw tick timestamp, so ``signals.ts`` / ``trades.entry_ts`` /
``trades.exit_ts`` reflect actual fill time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, Field

from app.strategies.base import (
    BarEvent,
    Signal,
    Strategy,
    TickEvent,
    in_entry_window,
)
from app.strategies.registry import register_strategy

_TAIPEI = ZoneInfo("Asia/Taipei")
_AUX_STALENESS = timedelta(minutes=15)


class TradeStrat15KParams(BaseModel):
    enable_short: bool = False

    kd_period: int = 9
    kd_k_smooth: int = 3
    kd_d_smooth: int = 3
    kd_long_floor: float = 75.0

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    dmi_period: int = 14

    tp_points: float = 130.0
    sl_points: float = 70.0
    trail_points: float = 80.0

    cooldown_seconds: int = Field(default=4500, ge=0)


@dataclass
class _PositionState:
    side: str
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
    if None in (
        close_curr, ma_prev, ma_curr,
        k_prev, d_prev, k_curr, d_curr,
        hist_prev, hist_curr,
        plus_prev, plus_curr, minus_prev, minus_curr,
    ):
        return False
    gate_ma = close_curr > ma_curr and ma_curr > ma_prev
    gate_kd = k_prev > d_prev and k_curr > d_curr and k_prev < kd_long_floor
    gate_macd = hist_prev < 0 and hist_curr > 0
    gate_dmi = (
        plus_prev > minus_prev
        and plus_curr > minus_curr
        and minus_curr < minus_prev
    )
    return gate_ma and gate_kd and gate_macd and gate_dmi


def _aux_macd_5m_positive(
    macd_5m: pd.DataFrame | None,
    ts: datetime,
) -> bool:
    """Auxiliary 5-minute MACD histogram > 0 gate.

    Returns False (block entry) when the aux indicator is missing, empty,
    stale by more than 15 minutes vs ``ts``, or has a non-positive last
    histogram value. The staleness guard covers FinMind outages where 5m
    bars stop arriving — without it, a stale positive snapshot would let
    entries through indefinitely.
    """
    if macd_5m is None or len(macd_5m) == 0:
        return False
    last_idx = macd_5m.index[-1]
    if isinstance(last_idx, pd.Timestamp):
        last_dt = last_idx.to_pydatetime()
    else:
        last_dt = last_idx  # already datetime-like
    # Both `ts` and the indicator index are tz-aware in production
    # (Timescale returns UTC-aware timestamps). Compare directly.
    if (ts - last_dt) >= _AUX_STALENESS:
        return False
    hist = _scalar(macd_5m["hist"], idx=-1) if "hist" in macd_5m.columns else None
    if hist is None:
        return False
    return hist > 0


@register_strategy
class TradeStrat15K(Strategy):
    name: ClassVar[str] = "strat_15k"
    display_name: ClassVar[str] = "15K策略"
    description: ClassVar[str] = (
        "15 分鐘多單策略；進場時段 09:15-12:15 / 15:00-24:00；進場：close>MA120 "
        "且 MA120 向上、KD 連兩 KS>DS 且第一根 KS<75、MACD 直方翻正、+DI>-DI 且 -DI 縮、"
        "5 分鐘 MACD 直方>0；出場：TP 130 / SL −70 / 移動停損 80。"
    )
    spec: ClassVar[dict[str, str]] = {
        "週期": "15 分鐘",
        "進場時段": "09:15-12:15 / 15:00-24:00 (Asia/Taipei)",
        "進場": (
            "close>MA120 且 MA120 向上；KD 連兩根 KS>DS 且第一根 KS<75；"
            "MACD 直方圖由負翻正；+DI>-DI 連兩根且第二根 -DI 縮；"
            "5 分鐘 MACD 直方>0 (近 15 分鐘內)"
        ),
        "出場": "獲利 130 點 / 虧損 70 點 / 移動停損 80 點",
        "冷卻": "出場後 5 根 15 分鐘 (4500 秒)",
        "備註": "僅多單；訊號逐筆即時觸發；出場不受時段與 5 分鐘 MACD 限制",
    }
    resolutions: ClassVar[list[str]] = ["15m"]
    tick_resolutions: ClassVar[list[str]] = ["15m"]
    params_schema: ClassVar[type[BaseModel]] = TradeStrat15KParams
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
        return self._evaluate(
            ev.bucket, close, ev.bars, ev.indicators, st, self.params,
            symbol=ev.symbol, resolution=ev.resolution,
        )

    def on_tick(self, ev: TickEvent) -> Signal | None:
        st = _state_for(self.name, ev.symbol)
        return self._evaluate(
            ev.ts, ev.price, ev.bars, ev.indicators, st, self.params,
            symbol=ev.symbol, resolution=ev.resolution,
        )

    def _evaluate(
        self,
        ts: datetime,
        price: float,
        bars: pd.DataFrame,
        indicators: dict[str, pd.DataFrame],
        st: _StratState,
        p: TradeStrat15KParams,
        *,
        symbol: str,
        resolution: str,
    ) -> Signal | None:
        # 1. Cooldown gate. While inside the window, suppress and re-arm latch
        # so the first tick after release re-evaluates as a rising edge.
        if st.cooldown_until is not None:
            if ts < st.cooldown_until:
                st.last_long_ready = False
                return None
            st.cooldown_until = None

        # 2. Manage open position — exits ignore window + 5m MACD gates so
        # any open position is closeable at all times.
        if st.position is not None:
            return self._manage_open_position(
                ts, price, indicators, st, p,
                symbol=symbol, resolution=resolution,
            )

        # 3. Maybe enter (window + 5m MACD + 4 primary gates).
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
        p: TradeStrat15KParams,
        *,
        symbol: str,
        resolution: str,
    ) -> Signal | None:
        pos = st.position
        if pos is None:
            return None

        if pos.side == "LONG":
            pnl = price - pos.entry_price
        else:
            pnl = pos.entry_price - price

        kd = indicators.get("kd")
        macd = indicators.get("macd")
        dmi = indicators.get("dmi")
        snapshot = _snapshot_ind(kd, macd, dmi)
        if all(v is None for v in snapshot.values()) and pos.entry_ind:
            snapshot = dict(pos.entry_ind)

        # Priority: TP → SL → TRAIL. (No DI_JUMP — that's strat_1k only.)
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
        p: TradeStrat15KParams,
        *,
        symbol: str,
        resolution: str,
    ) -> Signal | None:
        # Window gate first — block entries outside [09:15, 12:15) ∪
        # [15:00, 24:00) Asia/Taipei. Reset latch so a window-reopen with
        # pre-aligned gates fires cleanly as a fresh rising edge.
        if not in_entry_window(ts, _TAIPEI):
            st.last_long_ready = False
            return None

        # 5m MACD gate — block on missing/empty/stale (>15min)/non-positive
        # histogram. Reset latch so a flip back to positive re-arms cleanly.
        macd_5m = indicators.get("macd_5m")
        if not _aux_macd_5m_positive(macd_5m, ts):
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

        # Pass tick `price` as `close_curr` so close>MA gate evaluates
        # against live tick price (intra-bar firing).
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
        p: TradeStrat15KParams,
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
        p: TradeStrat15KParams,
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
