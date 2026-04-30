"""TAIEX Multi-Timeframe Strategy v2 (5-minute strategy).

Per spec, the timeframes are strictly partitioned:
  * 5m bar close = entry decision only (KD>20, MACD rising-edge above 0,
    +DI>21 AND +DI > -DI). NO TP/SL evaluation on the 5m path.
  * 1m bar close = TP/SL evaluation only (pure price math against the
    open position). NO entry logic on the 1m series.
  * 3m bar close = exit assist via -DI >= 23 momentum flip.

Trend layer  : Daily — display-only "Daily Confidence" badge (0/3..3/3),
               does not block entry.

Discipline:
  * No pyramiding (1 contract).
  * Cooldown 5 x 5m bars after exit before re-entry (cooldown_bars).
  * Freshness — only enter on rising edge (conditions just turned true).
  * Fill at signal bar close (framework cannot delay to next-bar open;
    documented deviation from spec).

Exits (any one):
  * +70 pt take-profit  (1m close vs entry_price)
  * -50 pt stop-loss    (1m close vs entry_price)
  * 3m -DI >= 23 (short-term momentum flip)

R:R = 70 : 50 = 1.4:1 (tighter than v1).

Strategy instance is rebuilt per bar_close, so position / cooldown state
lives in the module-level _STATE dict keyed by (strategy_name, symbol).

Indicator availability per resolution:
  * 5m: KD / MACD / DMI precomputed (entry layer + exit_ind snapshot
    fallback for 1m TP/SL exits).
  * 3m: DMI precomputed (exit assist).
  * 1m: no indicator_specs declared — TP/SL is pure price math, and the
    `_check_tp_sl_minute` exit_ind snapshot falls back to the most
    recent 5m indicator snapshot cached in module state at entry time.
  * 1d: KD / MACD / DMI precomputed (daily confidence).
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


class TradeStratV2Params(BaseModel):
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

    tp_points: float = 70.0
    sl_points: float = 50.0

    cooldown_bars: int = Field(default=5, ge=0)


@dataclass
class _PositionState:
    side: str  # "LONG" | "SHORT"
    entry_price: float
    entry_ts: datetime
    # Snapshot of the 5m indicator values at entry — reused as the
    # `exit_ind` payload when the 1m TP/SL path fires (1m has no
    # precomputed indicators). Fixed 8-key shape per `_snapshot_ind`.
    entry_ind: dict[str, float | None] = field(default_factory=dict)


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


def _macd_rising_edge(macd_series: pd.Series) -> bool:
    """Return True iff MACD just turned positive on the latest bar.

    Spec: `macd[-3] <= 0 and macd[-2] > 0 and macd[-1] > macd[-2]`.
    Pure helper — copied (not imported) so each strategy module is
    self-contained.
    """
    if macd_series is None or len(macd_series) < 3:
        return False
    m_3 = _scalar(macd_series, idx=-3)
    m_2 = _scalar(macd_series, idx=-2)
    m_1 = _scalar(macd_series, idx=-1)
    if None in (m_3, m_2, m_1):
        return False
    return m_3 <= 0 and m_2 > 0 and m_1 > m_2


def _snapshot_ind(
    kd: pd.DataFrame | None,
    macd: pd.DataFrame | None,
    dmi: pd.DataFrame | None,
) -> dict[str, float | None]:
    """Snapshot the latest KD / MACD / DMI scalars into a fixed-shape dict.

    Returns a dict that *always* contains all 8 keys (k, d, macd, signal,
    hist, plus_di, minus_di, adx). Missing / NaN values are emitted as
    ``None`` so the payload shape is stable for the frontend
    ``TradeIndicators`` type and the AI insight serializer. Numeric
    values are rounded to 2 decimals.
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


@register_strategy
class TradeStratV2(Strategy):
    name: ClassVar[str] = "trade_strat_v2"
    display_name: ClassVar[str] = "5分鐘策略"
    resolutions: ClassVar[list[str]] = ["1m", "3m", "5m", "1d"]
    params_schema: ClassVar[type[BaseModel]] = TradeStratV2Params
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
        params: TradeStratV2Params = self.params  # type: ignore[assignment]

        if ev.resolution == "1d":
            self._update_daily_confidence(ev, st, params)
            return None
        if ev.resolution == "1m":
            return self._check_tp_sl_minute(ev, st, params)
        if ev.resolution == "3m":
            return self._exit_assist(ev, st, params)
        if ev.resolution == "5m":
            return self._on_entry(ev, st, params)
        return None

    # ─── Daily confidence (display only) ─────────────────────────────────

    def _update_daily_confidence(
        self, ev: BarEvent, st: _StratState, p: TradeStratV2Params
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

    # ─── 5m entry (rising-edge gated) ────────────────────────────────────

    def _on_entry(
        self, ev: BarEvent, st: _StratState, p: TradeStratV2Params
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

        macd_rising = _macd_rising_edge(macd["macd"])
        # Mirror v1: symmetric falling-edge gate for SHORT (negate the
        # series so the same "just turned positive" helper detects the
        # downside cross).
        macd_falling = _macd_rising_edge(-macd["macd"])

        long_now = (
            k_curr > p.kd_long_floor
            and d_curr > p.kd_long_floor
            and macd_curr > 0
            and macd_rising
            and plus_curr > p.di_long_threshold
            and plus_curr > minus_curr
        )
        short_now = (
            p.enable_short
            and k_curr < p.kd_short_ceiling
            and d_curr < p.kd_short_ceiling
            and macd_curr < 0
            and macd_falling
            and minus_curr > p.di_short_threshold
            and minus_curr > plus_curr
        )

        # Per spec, V2 TP/SL runs on the 1m timeframe (`_check_tp_sl_minute`),
        # NOT on the 5m entry path. The 5m bar close evaluates entry only;
        # if a position is already open we just refresh the rising-edge
        # latches and return without emitting an exit.
        if st.position is not None:
            st.last_long_ready = long_now
            st.last_short_ready = short_now
            return None

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
                kd=kd, macd=macd, dmi=dmi,
            )
        if short_rising:
            return self._open_position(
                ev, st, side="SHORT", price=close_curr, p=p,
                kd=kd, macd=macd, dmi=dmi,
            )
        return None

    # ─── 1m TP / SL eval (pure price; no entry logic) ────────────────────

    def _check_tp_sl_minute(
        self, ev: BarEvent, st: _StratState, p: TradeStratV2Params
    ) -> Signal | None:
        pos = st.position
        if pos is None:
            return None
        close = _scalar(ev.bars["close"])
        if close is None:
            return None

        if pos.side == "LONG":
            pnl = close - pos.entry_price
        else:
            pnl = pos.entry_price - close

        # 1m has no indicator_specs by design — TP/SL is pure price.
        # Use whatever indicators the framework happened to attach; if
        # the live snapshot is entirely empty (all 8 keys None), fall
        # back to the entry-time 5m snapshot stored on the position.
        kd = ev.indicators.get("kd")
        macd = ev.indicators.get("macd")
        dmi = ev.indicators.get("dmi")
        snapshot = _snapshot_ind(kd, macd, dmi)
        if all(v is None for v in snapshot.values()) and pos.entry_ind:
            snapshot = dict(pos.entry_ind)

        if pnl >= p.tp_points:
            return self._close_position(
                ev, st, close, "TP", pnl, exit_ind=snapshot
            )
        if pnl <= -p.sl_points:
            return self._close_position(
                ev, st, close, "SL", pnl, exit_ind=snapshot
            )
        return None

    # ─── 3m exit assist (-DI >= exit_di_threshold) ───────────────────────

    def _exit_assist(
        self, ev: BarEvent, st: _StratState, p: TradeStratV2Params
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
        # NOTE: v2 spec uses `>=` (v1 uses `>`).
        flip_ind = minus if st.position.side == "LONG" else plus
        if flip_ind >= p.exit_di_threshold:
            pnl = (
                close - st.position.entry_price
                if st.position.side == "LONG"
                else st.position.entry_price - close
            )
            kd = ev.indicators.get("kd")
            macd = ev.indicators.get("macd")
            return self._close_position(
                ev, st, close, "DI_FLIP", pnl,
                exit_ind=_snapshot_ind(kd, macd, dmi),
            )
        return None

    # ─── helpers ─────────────────────────────────────────────────────────

    def _open_position(
        self,
        ev: BarEvent,
        st: _StratState,
        *,
        side: str,
        price: float,
        p: TradeStratV2Params,
        kd: pd.DataFrame,
        macd: pd.DataFrame,
        dmi: pd.DataFrame,
    ) -> Signal:
        snap = _snapshot_ind(kd, macd, dmi)
        st.position = _PositionState(
            side=side, entry_price=price, entry_ts=ev.bucket, entry_ind=dict(snap)
        )
        # Legacy `entry` payload key (back-compat for fixtures / V1 UI).
        legacy_entry = {
            "k": snap.get("k"),
            "d": snap.get("d"),
            "macd": snap.get("macd"),
            "di": snap.get("plus_di") if side == "LONG" else snap.get("minus_di"),
        }
        # Render-time NaN-safe float coercion for the human-readable reason.
        nan = float("nan")
        k_disp = snap.get("k") if snap.get("k") is not None else nan
        d_disp = snap.get("d") if snap.get("d") is not None else nan
        macd_disp = snap.get("macd") if snap.get("macd") is not None else nan
        di_disp = legacy_entry["di"] if legacy_entry["di"] is not None else nan
        return Signal(
            ts=ev.bucket,
            symbol=ev.symbol,
            resolution=ev.resolution,
            strategy=self.name,
            side=side,
            price=price,
            reason=(
                f"entry {side}: K={k_disp:.1f} D={d_disp:.1f} "
                f"MACD={macd_disp:.2f} DI={di_disp:.1f}"
            ),
            payload={
                "entry": legacy_entry,
                "entry_ind": snap,
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
        *,
        exit_ind: dict[str, float | None] | None = None,
    ) -> Signal:
        pos = st.position
        st.position = None
        st.cooldown_left = self.params.cooldown_bars  # type: ignore[attr-defined]
        st.last_long_ready = False
        st.last_short_ready = False
        # Always emit the fixed 8-key shape so the payload schema is
        # stable across exit paths (3m DI flip, 1m TP/SL, fallback).
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
