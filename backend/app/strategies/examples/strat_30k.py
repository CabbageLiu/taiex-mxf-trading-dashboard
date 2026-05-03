"""TAIEX 30-minute Strategy (30K策略).

Single-resolution LONG-only strategy. Entry on 30m bar close when 4 gates
align on a rising-edge transition (false → true). Exit on TP / SL / trailing
stop, evaluated on each subsequent 30m bar close.

Entry gates (all four must hold; LONG only):
  1. close > MA120 AND MA120 rising (`ma[-1] > ma[-2]`).
  2. KD: `k[-2] > d[-2]` AND `k[-1] > d[-1]` AND `k[-2] < 80`.
  3. MACD histogram: `hist[-2] < 0` AND `hist[-1] > 0`.
  4. DMI: `plus[-2] > minus[-2]` AND `plus[-1] > minus[-1]` AND
     `minus[-1] < minus[-2]`.

Exit priority (per 30m bar close, first match wins):
  TP   — pnl ≥ 180
  SL   — pnl ≤ −70
  TRAIL — pnl ≤ peak_pnl − 80 (peak tracked from entry, starts at 0;
          updated AFTER all exit checks)

Cooldown: 5 bars after EXIT. Decremented at start of `on_bar`. Resets
`last_long_ready` so the rising-edge latch re-arms.

Strategy instance is rebuilt per `bar_close`; state lives in module-level
`_STATE: dict[(name, symbol), _StratState]`. The strict `_STATE` naming is
required by the backtest engine's snapshot/restore introspection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel, Field

from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.registry import register_strategy


class TradeStrat30KParams(BaseModel):
    enable_short: bool = False

    kd_period: int = 9
    kd_k_smooth: int = 3
    kd_d_smooth: int = 3
    kd_long_floor: float = 80.0  # the `< 80` ceiling on the first KS

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    dmi_period: int = 14

    tp_points: float = 180.0
    sl_points: float = 70.0
    trail_points: float = 80.0

    cooldown_bars: int = Field(default=5, ge=0)


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
    cooldown_left: int = 0
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
    """Evaluate the four-gate LONG entry condition for the current bar."""
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


@register_strategy
class TradeStrat30K(Strategy):
    name: ClassVar[str] = "strat_30k"
    display_name: ClassVar[str] = "30K策略"
    description: ClassVar[str] = (
        "30 分鐘多單策略；進場：close>MA120 且 MA120 向上、KD 連兩 KS>DS、"
        "MACD 直方翻正、+DI>-DI 且 -DI 縮；出場：TP 180 / SL −70 / 移動停損 80。"
    )
    spec: ClassVar[dict[str, str]] = {
        "週期": "30 分鐘",
        "進場": (
            "close>MA120 且 MA120 向上；KD 連兩根 KS>DS 且第一根 KS<80；"
            "MACD 直方圖由負翻正；+DI>-DI 連兩根且第二根 -DI 縮"
        ),
        "出場": "獲利 180 點 / 虧損 70 點 / 移動停損 80 點",
        "冷卻": "出場後 5 根 30 分鐘 K 線",
        "備註": "僅多單；訊號於 K 線收盤觸發",
    }
    resolutions: ClassVar[list[str]] = ["30m"]
    params_schema: ClassVar[type[BaseModel]] = TradeStrat30KParams
    indicator_specs: ClassVar[dict[str, dict]] = {
        "ma120": {"kind": "ma", "params": {"period": 120, "kind": "sma"}},
        "kd": {"kind": "kd", "params": {"period": 9, "k_smooth": 3, "d_smooth": 3}},
        "macd": {"kind": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
        "dmi": {"kind": "dmi", "params": {"period": 14}},
    }

    @classmethod
    def dump_state(cls, symbol: str) -> dict:
        st = _STATE.get((cls.name, symbol))
        if st is None:
            return {}
        pos = st.position
        return {
            "cooldown_left": st.cooldown_left,
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
        params: TradeStrat30KParams = self.params  # type: ignore[assignment]

        if st.position is not None:
            return self._manage_position(ev, st, params)
        return self._maybe_enter(ev, st, params)

    # ─── exit / position management ──────────────────────────────────────

    def _manage_position(
        self, ev: BarEvent, st: _StratState, p: TradeStrat30KParams
    ) -> Signal | None:
        pos = st.position
        if pos is None:
            return None
        close = _scalar(ev.bars["close"])
        if close is None:
            return None

        # LONG-only by default; conditional kept for future SHORT support.
        if pos.side == "LONG":
            pnl = close - pos.entry_price
        else:
            pnl = pos.entry_price - close

        kd = ev.indicators.get("kd")
        macd = ev.indicators.get("macd")
        dmi = ev.indicators.get("dmi")
        snapshot = _snapshot_ind(kd, macd, dmi)
        if all(v is None for v in snapshot.values()) and pos.entry_ind:
            snapshot = dict(pos.entry_ind)

        # Priority order: TP → SL → TRAIL.
        if pnl >= p.tp_points:
            return self._close_position(ev, st, close, "TP", pnl, exit_ind=snapshot)
        if pnl <= -p.sl_points:
            return self._close_position(ev, st, close, "SL", pnl, exit_ind=snapshot)
        if pnl <= pos.peak_pnl - p.trail_points:
            return self._close_position(
                ev, st, close, "TRAIL", pnl, exit_ind=snapshot
            )

        # No exit fired — refresh peak and hold.
        pos.peak_pnl = max(pos.peak_pnl, pnl)
        return None

    # ─── entry (rising-edge gated) ───────────────────────────────────────

    def _maybe_enter(
        self, ev: BarEvent, st: _StratState, p: TradeStrat30KParams
    ) -> Signal | None:
        ma = ev.indicators.get("ma120")
        kd = ev.indicators.get("kd")
        macd = ev.indicators.get("macd")
        dmi = ev.indicators.get("dmi")
        if ma is None or kd is None or macd is None or dmi is None:
            return None

        close_curr = _scalar(ev.bars["close"])
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

        long_now = _long_entry_now(
            close_curr, ma_prev, ma_curr,
            k_prev, d_prev, k_curr, d_curr,
            hist_prev, hist_curr,
            plus_prev, plus_curr, minus_prev, minus_curr,
            kd_long_floor=p.kd_long_floor,
        )

        long_rising = long_now and not st.last_long_ready
        st.last_long_ready = long_now

        # Cooldown blocks new entries AND consumes one bar each call. Decrement
        # happens inside the block so the bar where cooldown_left was just set
        # by _close_position is still counted as a blocked bar on its OWN turn
        # and does not double-decrement on the exit bar (no _maybe_enter call
        # while a position is open).
        if st.cooldown_left > 0:
            st.cooldown_left -= 1
            return None
        if not long_rising:
            return None
        if close_curr is None:
            return None

        return self._open_position(
            ev, st, side="LONG", price=close_curr, p=p,
            kd=kd, macd=macd, dmi=dmi,
        )

    # ─── helpers ─────────────────────────────────────────────────────────

    def _open_position(
        self,
        ev: BarEvent,
        st: _StratState,
        *,
        side: str,
        price: float,
        p: TradeStrat30KParams,
        kd: pd.DataFrame,
        macd: pd.DataFrame,
        dmi: pd.DataFrame,
    ) -> Signal:
        snap = _snapshot_ind(kd, macd, dmi)
        st.position = _PositionState(
            side=side, entry_price=price, entry_ts=ev.bucket,
            entry_ind=dict(snap), peak_pnl=0.0,
        )
        nan = float("nan")
        k_disp = snap.get("k") if snap.get("k") is not None else nan
        d_disp = snap.get("d") if snap.get("d") is not None else nan
        macd_disp = snap.get("macd") if snap.get("macd") is not None else nan
        plus_disp = snap.get("plus_di") if snap.get("plus_di") is not None else nan
        return Signal(
            ts=ev.bucket,
            symbol=ev.symbol,
            resolution=ev.resolution,
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
                "fill_hint": "bar_close",
            },
        )

    def _close_position(
        self,
        ev: BarEvent,
        st: _StratState,
        price: float,
        reason: str,
        pnl: float,
        *,
        exit_ind: dict[str, float | None] | None = None,
    ) -> Signal:
        pos = st.position
        st.position = None
        st.cooldown_left = self.params.cooldown_bars  # type: ignore[attr-defined]
        st.last_long_ready = False
        if exit_ind is None:
            exit_ind = _snapshot_ind(None, None, None)
        return Signal(
            ts=ev.bucket,
            symbol=ev.symbol,
            resolution=ev.resolution,
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
                "fill_hint": "bar_close",
            },
        )
