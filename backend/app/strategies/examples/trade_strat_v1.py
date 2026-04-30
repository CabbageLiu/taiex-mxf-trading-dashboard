"""TAIEX Multi-Timeframe Strategy v1 (30分鐘線策略).

Entry layer  : 30m (KD>20, MACD rising-edge positive, +DI>21 AND +DI>-DI).
Exit assist  : 3m -DI > 23 (short-term momentum flip).
Trend layer  : Daily — display-only "Daily Confidence" badge (0/3..3/3),
               does not block entry.

MACD rising-edge gate
---------------------
Per spec: "MACD above 0 (initial 3 figures of MACD trend becoming positive)".
Interpreted as a 3-bar pattern on the entry timeframe (30m):
    macd[-3] <= 0 AND macd[-2] > 0 AND macd[-1] > macd[-2]
i.e. was non-positive 3 bars ago, became positive on the middle bar, and
kept rising on the latest bar. NaN values are treated as 0 / non-positive.

Daily confidence rule (display only)
------------------------------------
Each of the 4 conditions, evaluated on the 1d bar, contributes 1 point per
side:
    1. KD > 20 (long) / KD < 80 (short)
    2. MACD > 0 (long) / MACD < 0 (short)
    3. +DI > 21 AND +DI > -DI  (long) / -DI > 21 AND -DI > +DI  (short)
The +DI condition mirrors the live entry gate so the badge stays
consistent with what 30m would actually fire on.

Discipline:
  * No pyramiding (1 contract).
  * Cooldown 5 x 30m bars after exit before re-entry.
  * Freshness — only enter on rising edge (conditions just turned true).
  * Fill at signal bar close (framework cannot delay to next-bar open;
    documented deviation from spec).

Exits:
  * +220 pt take-profit
  * -60 pt stop-loss
  * 3m -DI > 23 (short-term momentum flip)

R:R = 220 : 60 = 3.67:1.

Strategy instance is rebuilt per bar_close, so position / cooldown state
lives in the module-level _STATE dict keyed by (strategy_name, symbol).

Indicator snapshots are persisted into Signal.payload at entry
(`entry_ind`) and exit (`exit_ind`) — full {k, d, macd, signal, hist,
plus_di, minus_di, adx} dict, rounded to 2 decimals, NaN → None.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel, Field

from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.registry import register_strategy


class TradeStratV1Params(BaseModel):
    enable_short: bool = False

    kd_period: int = 9
    kd_long_floor: float = 20.0
    kd_short_ceiling: float = 80.0

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    dmi_period: int = 14
    di_long_threshold: float = 21.0
    di_short_threshold: float = 21.0
    exit_di_threshold: float = 23.0

    tp_points: float = 220.0
    sl_points: float = 60.0

    cooldown_bars: int = Field(default=5, ge=0)


@dataclass
class _PositionState:
    side: str  # "LONG" | "SHORT"
    entry_price: float
    entry_ts: datetime


@dataclass
class _StratState:
    position: _PositionState | None = None
    cooldown_left: int = 0
    last_long_ready: bool = False
    last_short_ready: bool = False
    daily_confidence_long: int = 0
    daily_confidence_short: int = 0
    daily_last_bucket: datetime | None = None


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


def _macd_just_turned_positive(macd_series: pd.Series) -> bool:
    """Rising-edge gate: macd was non-positive 3 bars ago, turned positive
    2 bars ago, and kept rising on the latest bar.

    NaN-safe: NaN values are treated as 0 (non-positive); a NaN at index -2
    or -1 cannot satisfy the "> 0" / strictly-rising checks.
    """
    if macd_series is None or len(macd_series) < 3:
        return False

    def _safe(idx: int) -> float | None:
        try:
            v = float(macd_series.iloc[idx])
        except (TypeError, ValueError):
            return None
        if math.isnan(v):
            return 0.0  # treat NaN as non-positive
        return v

    m3 = _safe(-3)
    m2 = _safe(-2)
    m1 = _safe(-1)
    if m3 is None or m2 is None or m1 is None:
        return False
    return m3 <= 0 and m2 > 0 and m1 > m2


def _ind_snapshot(
    indicators: dict[str, pd.DataFrame], idx: int
) -> dict[str, float | None]:
    """Snapshot KD/MACD/DMI values at integer position ``idx``. NaN → None.

    Keys: k, d, macd, signal, hist, plus_di, minus_di, adx.
    Numeric values are rounded to 2 decimals.
    """
    keys = ("k", "d", "macd", "signal", "hist", "plus_di", "minus_di", "adx")
    snap: dict[str, float | None] = dict.fromkeys(keys)

    def _read(df: pd.DataFrame | None, col: str) -> float | None:
        if df is None or col not in df.columns:
            return None
        try:
            val = df[col].iloc[idx]
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

    kd = indicators.get("kd")
    macd = indicators.get("macd")
    dmi = indicators.get("dmi")
    snap["k"] = _read(kd, "k")
    snap["d"] = _read(kd, "d")
    snap["macd"] = _read(macd, "macd")
    snap["signal"] = _read(macd, "signal")
    snap["hist"] = _read(macd, "hist")
    snap["plus_di"] = _read(dmi, "plus_di")
    snap["minus_di"] = _read(dmi, "minus_di")
    snap["adx"] = _read(dmi, "adx")
    return snap


@register_strategy
class TradeStratV1(Strategy):
    name: ClassVar[str] = "trade_strat_v1"
    display_name: ClassVar[str] = "30分鐘線策略"
    resolutions: ClassVar[list[str]] = ["3m", "30m", "1d"]
    params_schema: ClassVar[type[BaseModel]] = TradeStratV1Params
    indicator_specs: ClassVar[dict[str, dict]] = {
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
            "daily_confidence_long": st.daily_confidence_long,
            "daily_confidence_short": st.daily_confidence_short,
            "daily_last_bucket": (
                st.daily_last_bucket.isoformat() if st.daily_last_bucket else None
            ),
            "cooldown_left": st.cooldown_left,
            "position": (
                {
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "entry_ts": pos.entry_ts.isoformat(),
                }
                if pos
                else None
            ),
        }

    def on_bar(self, ev: BarEvent) -> Signal | None:
        st = _state_for(self.name, ev.symbol)
        params: TradeStratV1Params = self.params  # type: ignore[assignment]

        if ev.resolution == "1d":
            self._update_daily_confidence(ev, st, params)
            return None
        if ev.resolution == "3m":
            return self._exit_assist(ev, st, params)
        if ev.resolution == "30m":
            return self._on_30m(ev, st, params)
        return None

    # ─── Daily confidence (display only) ─────────────────────────────────

    def _update_daily_confidence(
        self, ev: BarEvent, st: _StratState, p: TradeStratV1Params
    ) -> None:
        kd = ev.indicators.get("kd")
        macd = ev.indicators.get("macd")
        dmi = ev.indicators.get("dmi")
        if kd is None or macd is None or dmi is None:
            return
        k = _scalar(kd["k"])
        d = _scalar(kd["d"])
        macd_val = _scalar(macd["macd"])
        plus_di = _scalar(dmi["plus_di"])
        minus_di = _scalar(dmi["minus_di"])
        if None in (k, d, macd_val, plus_di, minus_di):
            return

        long_score = sum(
            (
                k > p.kd_long_floor and d > p.kd_long_floor,
                macd_val > 0,
                plus_di > p.di_long_threshold and plus_di > minus_di,
            )
        )
        short_score = sum(
            (
                k < p.kd_short_ceiling and d < p.kd_short_ceiling,
                macd_val < 0,
                minus_di > p.di_short_threshold and minus_di > plus_di,
            )
        )
        st.daily_confidence_long = long_score
        st.daily_confidence_short = short_score
        st.daily_last_bucket = ev.bucket

    # ─── 30m entry / SL / TP ─────────────────────────────────────────────

    def _on_30m(
        self, ev: BarEvent, st: _StratState, p: TradeStratV1Params
    ) -> Signal | None:
        if st.cooldown_left > 0:
            st.cooldown_left -= 1

        kd = ev.indicators.get("kd")
        macd = ev.indicators.get("macd")
        dmi = ev.indicators.get("dmi")
        if kd is None or macd is None or dmi is None:
            return None

        k_curr = _scalar(kd["k"])
        d_curr = _scalar(kd["d"])
        macd_curr = _scalar(macd["macd"])
        plus_curr = _scalar(dmi["plus_di"])
        minus_curr = _scalar(dmi["minus_di"])
        close_curr = _scalar(ev.bars["close"])
        if None in (k_curr, d_curr, macd_curr, plus_curr, minus_curr, close_curr):
            return None

        macd_rising = _macd_just_turned_positive(macd["macd"])
        macd_falling = _macd_just_turned_positive(-macd["macd"])  # mirror for SHORT

        long_now = (
            k_curr > p.kd_long_floor
            and d_curr > p.kd_long_floor
            and macd_rising
            and plus_curr > p.di_long_threshold
            and plus_curr > minus_curr
        )
        short_now = (
            p.enable_short
            and k_curr < p.kd_short_ceiling
            and d_curr < p.kd_short_ceiling
            and macd_falling
            and minus_curr > p.di_short_threshold
            and minus_curr > plus_curr
        )

        # Evaluate exit on existing position before any new entry.
        if st.position is not None:
            sig = self._check_tp_sl(ev, st, p, close_curr)
            if sig is not None:
                st.last_long_ready = long_now
                st.last_short_ready = short_now
                return sig

        # Freshness — rising edge only.
        long_rising = long_now and not st.last_long_ready
        short_rising = short_now and not st.last_short_ready
        st.last_long_ready = long_now
        st.last_short_ready = short_now

        if st.position is not None or st.cooldown_left > 0:
            return None

        if long_rising:
            return self._open_position(
                ev, st, side="LONG", price=close_curr, p=p,
                k=k_curr, d=d_curr, macd_v=macd_curr, di=plus_curr,
            )
        if short_rising:
            return self._open_position(
                ev, st, side="SHORT", price=close_curr, p=p,
                k=k_curr, d=d_curr, macd_v=macd_curr, di=minus_curr,
            )
        return None

    def _check_tp_sl(
        self,
        ev: BarEvent,
        st: _StratState,
        p: TradeStratV1Params,
        close: float,
    ) -> Signal | None:
        pos = st.position
        if pos is None:
            return None
        if pos.side == "LONG":
            pnl = close - pos.entry_price
        else:
            pnl = pos.entry_price - close
        if pnl >= p.tp_points:
            return self._close_position(ev, st, close, "TP", pnl)
        if pnl <= -p.sl_points:
            return self._close_position(ev, st, close, "SL", pnl)
        return None

    # ─── 3m exit assist (-DI flip) ───────────────────────────────────────

    def _exit_assist(
        self, ev: BarEvent, st: _StratState, p: TradeStratV1Params
    ) -> Signal | None:
        if st.position is None:
            return None
        dmi = ev.indicators.get("dmi")
        close = _scalar(ev.bars["close"])
        if dmi is None or close is None:
            return None
        plus = _scalar(dmi["plus_di"])
        minus = _scalar(dmi["minus_di"])
        if plus is None or minus is None:
            return None

        # LONG → exit when -DI flips strong (downside momentum).
        # SHORT → exit when +DI flips strong (upside momentum).
        flip_ind = minus if st.position.side == "LONG" else plus
        if flip_ind > p.exit_di_threshold:
            pnl = (
                close - st.position.entry_price
                if st.position.side == "LONG"
                else st.position.entry_price - close
            )
            return self._close_position(ev, st, close, "DI_FLIP", pnl)
        return None

    # ─── helpers ─────────────────────────────────────────────────────────

    def _open_position(
        self,
        ev: BarEvent,
        st: _StratState,
        *,
        side: str,
        price: float,
        p: TradeStratV1Params,
        k: float,
        d: float,
        macd_v: float,
        di: float,
    ) -> Signal:
        st.position = _PositionState(side=side, entry_price=price, entry_ts=ev.bucket)
        entry_ind = _ind_snapshot(ev.indicators, -1)
        return Signal(
            ts=ev.bucket,
            symbol=ev.symbol,
            resolution=ev.resolution,
            strategy=self.name,
            side=side,
            price=price,
            reason=(
                f"entry {side}: K={k:.1f} D={d:.1f} MACD={macd_v:.2f} DI={di:.1f}"
            ),
            payload={
                # Legacy back-compat: tests / fixtures still read `entry`.
                "entry": {
                    "k": round(k, 2),
                    "d": round(d, 2),
                    "macd": round(macd_v, 2),
                    "di": round(di, 2),
                },
                "entry_ind": entry_ind,
                "tp_points": p.tp_points,
                "sl_points": p.sl_points,
                "daily_confidence_long": st.daily_confidence_long,
                "daily_confidence_short": st.daily_confidence_short,
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
    ) -> Signal:
        pos = st.position
        st.position = None
        st.cooldown_left = self.params.cooldown_bars  # type: ignore[attr-defined]
        st.last_long_ready = False
        st.last_short_ready = False
        exit_ind = _ind_snapshot(ev.indicators, -1)
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
