"""TAIEX Multi-Timeframe Strategy v1.

Entry layer  : 30m K (KD>20, MACD>0, +DI>21).
Exit assist  : 3m -DI>23 — substituted with 5m here (3m not in RESOLUTIONS).
Trend layer  : Daily — display-only "Daily Confidence" badge (0/3..3/3),
               does not block entry.

Discipline:
  * No pyramiding (1 contract).
  * Cooldown 5 x 30m bars after exit before re-entry.
  * Freshness — only enter on rising edge (conditions just turned true).
  * Fill at signal bar close (framework cannot delay to next-bar open;
    documented deviation from spec).

Exits:
  * +220 pt take-profit
  * -60 pt stop-loss
  * 5m -DI > 23 (short-term momentum flip)

R:R = 220 : 60 = 3.67:1.

Strategy instance is rebuilt per bar_close, so position / cooldown state
lives in the module-level _STATE dict keyed by (strategy_name, symbol).
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


@register_strategy
class TradeStratV1(Strategy):
    name: ClassVar[str] = "trade_strat_v1"
    resolutions: ClassVar[list[str]] = ["5m", "30m", "1d"]
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
        if ev.resolution == "5m":
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
                plus_di > p.di_long_threshold,
            )
        )
        short_score = sum(
            (
                k < p.kd_short_ceiling and d < p.kd_short_ceiling,
                macd_val < 0,
                minus_di > p.di_short_threshold,
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

        long_now = (
            k_curr > p.kd_long_floor
            and d_curr > p.kd_long_floor
            and macd_curr > 0
            and plus_curr > p.di_long_threshold
        )
        short_now = (
            p.enable_short
            and k_curr < p.kd_short_ceiling
            and d_curr < p.kd_short_ceiling
            and macd_curr < 0
            and minus_curr > p.di_short_threshold
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

    # ─── 5m exit assist (substituting for 3m -DI flip) ───────────────────

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
                "entry": {
                    "k": round(k, 2),
                    "d": round(d, 2),
                    "macd": round(macd_v, 2),
                    "di": round(di, 2),
                },
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
                "fill_hint": "bar_close",
            },
        )
