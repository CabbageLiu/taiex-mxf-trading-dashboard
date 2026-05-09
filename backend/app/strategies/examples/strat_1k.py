"""TAIEX 1-minute Strategy (1K策略).

Single-resolution LONG-only strategy. Entry fires the moment the gates
align intra-bar (tick-driven); exits (TP / TRAIL) fire the moment the
tick price crosses the threshold for the active time-of-day bucket. Both
``on_bar`` (back-compat / backtest path) and ``on_tick`` route through
the same ``_evaluate`` helper.

Entry gates (LONG only — all must hold on the same evaluation):
  0. Entry window: Asia/Taipei time inside
     [08:45, 13:45) ∪ [15:00, 05:00 next-day). The night window wraps
     past midnight; ``in_entry_window`` handles the wrap when
     ``night_close < night_open``.
  1. 1m DMI rising: ``plus_di[-1] > plus_di[-2]`` AND
     ``minus_di[-1] < minus_di[-2]``.
  2. 1m KD: ``k[-2] > d[-2]`` AND ``k[-1] > d[-1]`` AND ``k[-2] < 70``.
  3. 1m MACD histogram positive: ``hist[-1] > 0`` (strict; equality at
     zero blocks).

Exit priority (per tick or bar close, first match wins):
  TP    — pnl ≥ tp_for_bucket
  TRAIL — pnl ≤ peak_pnl − 40 (peak tracked from entry, starts at 0)

TP is a function of the Taipei wall-clock time of the evaluation
timestamp (half-open intervals, mirroring ``in_entry_window``):

  [08:45, 10:31)  →  TP=50
  [10:31, 13:45)  →  TP=40
  [15:00, 18:01)  →  TP=30
  [18:01, 23:31)  →  TP=50
  [23:31, 24:00) ∪ [00:00, 05:00)  →  TP=30
  closed gaps [13:45, 15:00) ∪ [05:00, 08:45)  →  TP=40 (fallback;
    market closed so this is rarely exercised)

TRAIL is uniformly 40 across all buckets.

Hard SL and the prior ``DI_JUMP_1M`` exit are removed: the trailing
stop alone caps downside (a 40-pt drop from peak — including a 40-pt
drawdown from the entry, which has peak_pnl=0 — closes the position).

Exits ignore the entry-window gate — open positions must remain
closeable any time, including the 13:45–15:00 day-close gap and the
05:00–08:45 morning gap.

Cooldown: 300 seconds after EXIT (time-based, not bar-counted). While
``ts < cooldown_until`` evaluation returns None and resets
``last_long_ready`` so the rising-edge latch re-arms once cooldown
clears. The window gate behaves the same way: when it blocks, the latch
resets so the first aligned tick after the window reopens fires
cleanly as a fresh rising edge.

Fill convention: tick-driven (not bar close). ``Signal.ts`` carries the
raw tick timestamp, so ``signals.ts`` / ``trades.entry_ts`` /
``trades.exit_ts`` reflect actual fill time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel, Field

from app.config import get_settings
from app.strategies.base import BarEvent, Signal, Strategy, TickEvent, in_entry_window
from app.strategies.registry import register_strategy

_DAY_OPEN = time(8, 45)
_DAY_CLOSE = time(13, 45)
_NIGHT_OPEN = time(15, 0)
_NIGHT_CLOSE = time(5, 0)  # overnight wrap; exclusive upper bound

_TRAIL_POINTS = 40.0  # uniform across all ToD buckets


class TradeStrat1KParams(BaseModel):
    enable_short: bool = False

    kd_period: int = 9
    kd_k_smooth: int = 3
    kd_d_smooth: int = 3
    kd_long_floor: float = 70.0  # first 1m K must be strictly below this

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    dmi_period: int = 14

    cooldown_seconds: int = Field(default=300, ge=0)


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
    k_prev: float | None,
    d_prev: float | None,
    k_curr: float | None,
    d_curr: float | None,
    hist_curr: float | None,
    plus_prev: float | None,
    plus_curr: float | None,
    minus_prev: float | None,
    minus_curr: float | None,
    kd_long_floor: float,
) -> bool:
    """Evaluate the 1-minute primary entry gates (all AND-ed).

    Returns True iff every gate holds:
      DMI rising — ``plus_curr > plus_prev`` AND ``minus_curr < minus_prev``.
      KD         — ``k_prev > d_prev`` AND ``k_curr > d_curr`` AND
                   ``k_prev < kd_long_floor``.
      MACD       — ``hist_curr > 0`` (strict).
    """
    if None in (
        k_prev, d_prev, k_curr, d_curr,
        hist_curr,
        plus_prev, plus_curr, minus_prev, minus_curr,
    ):
        return False
    if not (plus_curr > plus_prev and minus_curr < minus_prev):
        return False
    if not (k_prev > d_prev and k_curr > d_curr and k_prev < kd_long_floor):
        return False
    if not (hist_curr > 0.0):
        return False
    return True


_DAY_SPLIT = time(10, 31)
_NIGHT_FIRST_END = time(18, 1)
_NIGHT_SECOND_END = time(23, 31)


def _exit_params_for(ts: datetime, tz: tzinfo) -> tuple[float, float]:
    """Return ``(tp_points, trail_points)`` for the Taipei-local ``ts``.

    ToD buckets (half-open):
      [08:45, 10:31)            → TP 50
      [10:31, 13:45)            → TP 40
      [15:00, 18:01)            → TP 30
      [18:01, 23:31)            → TP 50
      [23:31, 24:00) ∪ [00:00, 05:00)  → TP 30
      otherwise (market-closed gaps)   → TP 40 (safe fallback)

    TRAIL is uniformly 40 across all buckets.
    """
    local = ts.astimezone(tz).time() if ts.tzinfo is not None else ts.time()
    if _DAY_OPEN <= local < _DAY_SPLIT:
        return 50.0, _TRAIL_POINTS
    if _DAY_SPLIT <= local < _DAY_CLOSE:
        return 40.0, _TRAIL_POINTS
    if _NIGHT_OPEN <= local < _NIGHT_FIRST_END:
        return 30.0, _TRAIL_POINTS
    if _NIGHT_FIRST_END <= local < _NIGHT_SECOND_END:
        return 50.0, _TRAIL_POINTS
    if local >= _NIGHT_SECOND_END or local < _NIGHT_CLOSE:
        return 30.0, _TRAIL_POINTS
    return 40.0, _TRAIL_POINTS


@register_strategy
class TradeStrat1K(Strategy):
    name: ClassVar[str] = "strat_1k"
    display_name: ClassVar[str] = "1K策略"
    description: ClassVar[str] = (
        "1 分鐘多單策略；開倉時段 08:45-13:45 / 15:00-05:00 (隔夜)；進場 (全部成立)："
        "1m +DI 上升 -DI 下降、1m KD 連兩 KS>DS 且首根 KS<70、1m MACD 直方>0；"
        "出場：依時段獲利 30/40/50 點；移動停損 40 點。"
    )
    spec: ClassVar[dict[str, str]] = {
        "週期": "1 分鐘",
        "開倉時段": "08:45-13:45 / 15:00-05:00 (隔夜) (Asia/Taipei)",
        "進場": (
            "1 分鐘 +DI 上升、-DI 下降；"
            "1 分鐘 KD 連兩 KS>DS 且第一根 KS<70；"
            "1 分鐘 MACD 直方圖>0 (全部成立)"
        ),
        "出場": (
            "獲利 (依時段): "
            "08:45-10:30 → 50 點；10:31-13:44 → 40 點；"
            "15:00-18:00 → 30 點；18:01-23:30 → 50 點；"
            "23:31-04:59 → 30 點；移動停損固定 40 點"
        ),
        "冷卻": "出場後 5 分鐘 (300 秒)",
        "備註": (
            "僅多單；訊號逐筆即時觸發；出場不受開倉時段限制 "
            "(隔夜 13:45-15:00 / 05:00-08:45 收盤空檔仍可出場)"
        ),
    }
    resolutions: ClassVar[list[str]] = ["1m"]
    tick_resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = TradeStrat1KParams
    indicator_specs: ClassVar[dict[str, dict]] = {
        "kd": {"kind": "kd", "params": {"period": 9, "k_smooth": 3, "d_smooth": 3}},
        "macd": {"kind": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
        "dmi": {"kind": "dmi", "params": {"period": 14}},
    }
    aux_indicator_specs: ClassVar[dict[str, dict]] = {}

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
        p: TradeStrat1KParams,
        *,
        symbol: str,
        resolution: str,
    ) -> Signal | None:
        if st.cooldown_until is not None:
            if ts < st.cooldown_until:
                st.last_long_ready = False
                return None
            st.cooldown_until = None

        if st.position is not None:
            return self._manage_open_position(
                ts, price, indicators, st, p,
                symbol=symbol, resolution=resolution,
            )

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
        p: TradeStrat1KParams,
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

        tp, trail = _exit_params_for(ts, get_settings().tz)

        if pnl >= tp:
            return self._close_position(
                ts, price, "TP", pnl,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )
        if pnl <= pos.peak_pnl - trail:
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
        p: TradeStrat1KParams,
        *,
        symbol: str,
        resolution: str,
    ) -> Signal | None:
        if not in_entry_window(
            ts,
            get_settings().tz,
            day_open=_DAY_OPEN,
            day_close=_DAY_CLOSE,
            night_open=_NIGHT_OPEN,
            night_close=_NIGHT_CLOSE,
        ):
            st.last_long_ready = False
            return None

        kd = indicators.get("kd")
        macd = indicators.get("macd")
        dmi = indicators.get("dmi")
        if dmi is None or kd is None or macd is None:
            return None

        k_prev = _scalar(kd["k"], idx=-2)
        d_prev = _scalar(kd["d"], idx=-2)
        k_curr = _scalar(kd["k"], idx=-1)
        d_curr = _scalar(kd["d"], idx=-1)
        hist_curr = _scalar(macd["hist"], idx=-1)
        plus_prev = _scalar(dmi["plus_di"], idx=-2)
        plus_curr = _scalar(dmi["plus_di"], idx=-1)
        minus_prev = _scalar(dmi["minus_di"], idx=-2)
        minus_curr = _scalar(dmi["minus_di"], idx=-1)

        long_now = _long_entry_now(
            k_prev, d_prev, k_curr, d_curr,
            hist_curr,
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
        p: TradeStrat1KParams,
        kd: pd.DataFrame | None,
        macd: pd.DataFrame | None,
        dmi: pd.DataFrame | None,
        symbol: str,
        resolution: str,
    ) -> Signal:
        snap = _snapshot_ind(kd, macd, dmi)
        st.position = _PositionState(
            side=side, entry_price=price, entry_ts=ts,
            entry_ind=dict(snap), peak_pnl=0.0,
        )
        tp_at_entry, trail_at_entry = _exit_params_for(ts, get_settings().tz)
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
                "tp_points": tp_at_entry,
                "trail_points": trail_at_entry,
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
        p: TradeStrat1KParams,
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
