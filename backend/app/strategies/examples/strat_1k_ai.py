"""TAIEX 1-minute Strategy — AI exit-refactor variant (1K策略AI版本).

Sibling of ``strat_1k`` with identical entry logic plus a SHORT-side
mirror. The exit stack is rebuilt to separate the two roles that the
single ``TRAIL`` rule was conflating in ``strat_1k``:

  - **Hard SL** (entry-time risk cap, optionally ATR-scaled)
  - **Break-even** (lock zero once enough profit accrued)
  - **Trailing stop** (arms only after profit threshold, then tight)
  - **Time cutoff** (scratch stale trades)
  - **Crash regime override** (force-exit on adverse DI thrust + ATR burst)

The TP table is reused verbatim from ``strat_1k`` so this is a clean
A/B against the live strategy on exit logic only.

Entry gates (LONG — identical to ``strat_1k``; SHORT mirrored):
  0. Entry window: Asia/Taipei in
     [08:45, 13:45) ∪ [15:00, 05:00 next-day).
  1. DMI direction:
       LONG:  ``+DI`` rising AND ``-DI`` falling.
       SHORT: ``-DI`` rising AND ``+DI`` falling.
  2. KD:
       LONG:  ``k_prev > d_prev`` AND ``k_curr > d_curr``
              AND ``k_prev < kd_long_floor`` (default 70).
       SHORT: ``k_prev < d_prev`` AND ``k_curr < d_curr``
              AND ``k_prev > kd_short_ceiling`` (default 30).
  3. MACD histogram:
       LONG:  ``hist > 0``.
       SHORT: ``hist < 0``.

If both LONG and SHORT latches fire on the same evaluation, LONG wins
(deterministic tie-break preserving strat_1k feel).

Exit priority (per tick or bar close, first match wins):
  EOW   — out of entry window
  CRASH — adverse DI spread + TR burst against position
  TP    — pnl ≥ tp_for_bucket (same table as strat_1k)
  SL    — pnl ≤ -sl_distance (ATR-scaled or fixed)
  BE    — peak_pnl ≥ be_trigger AND pnl ≤ 0
  TRAIL — peak_pnl ≥ trail_arm AND pnl ≤ peak_pnl - trail_give_back
  TIME  — age ≥ time_cutoff_minutes AND pnl < time_cutoff_min_pnl

All thresholds live in ``TradeStrat1KAIParams`` and are tunable via
``PATCH /strategies/strat_1k_ai/params``.

Cooldown + fill convention identical to ``strat_1k``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.strategies.base import BarEvent, Signal, Strategy, TickEvent, in_entry_window
from app.strategies.examples.strat_1k import (
    _DAY_CLOSE,
    _DAY_OPEN,
    _NIGHT_CLOSE,
    _NIGHT_OPEN,
    _exit_params_for,
)
from app.strategies.registry import register_strategy


class TradeStrat1KAIParams(BaseModel):
    # --- entry (identical defaults to strat_1k except enable_short)
    enable_short: bool = True

    kd_period: int = 9
    kd_k_smooth: int = 3
    kd_d_smooth: int = 3
    kd_long_floor: float = 70.0
    kd_short_ceiling: float = 30.0

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    dmi_period: int = 14

    cooldown_seconds: int = Field(default=300, ge=0)

    # --- exit / risk
    # Volatility-aware hard SL. When enabled SL distance is
    # ``clamp(atr_at_entry * atr_sl_mult, floor=atr_sl_floor, cap=atr_sl_cap)``.
    use_atr_sl: bool = True
    atr_period: int = 14
    atr_sl_mult: float = 1.8
    atr_sl_floor: float = 18.0
    atr_sl_cap: float = 45.0

    # Fixed-point fallback when ``use_atr_sl`` is False.
    hard_sl_points: float = 30.0

    # Break-even stop: once peak_pnl crosses trigger, exit if pnl drops to 0.
    # ``be_enabled=False`` skips BE entirely (mid-flight position protection
    # falls back to hard SL / TRAIL / TIME). Per 2026-05-20 calibration: BE @
    # +15 on 1m TAIEX sat inside noise and destroyed ~5pt/trade of expectancy.
    be_enabled: bool = True
    be_trigger_points: float = 15.0

    # Trailing stop arms only after ``trail_arm_points`` of unrealised profit.
    # Per-side enable flags let us keep TRAIL on the side where it earns
    # alpha and disable it where it just gives back winners. Same 2026-05-20
    # calibration found LONG TRAIL contributed +84/8 (marginal) while SHORT
    # TRAIL carried +135/13 of the SHORT-side edge.
    trail_long_enabled: bool = True
    trail_short_enabled: bool = True
    trail_arm_points: float = 25.0
    trail_give_back_points: float = 18.0

    # Time-based cutoff: scratch the trade if it hasn't done anything by then.
    time_cutoff_minutes: int = Field(default=30, ge=0)
    time_cutoff_min_pnl: float = 5.0

    # Crash-regime override: force-exit on adverse DI thrust + TR burst.
    crash_regime_exit: bool = True
    crash_di_spread_threshold: float = 8.0
    crash_atr_burst_mult: float = 2.0

    # 1m Bollinger-Band width pre-entry filter. Skip entries when the
    # primary-resolution BB band-width (upper − lower)/close exceeds the
    # threshold — empirically the only Bonferroni-passing micro-vol gate
    # that survives regime flips (2026-05-20 study).
    bb_width_filter_enabled: bool = False
    bb_width_max_pct: float = 0.0035
    bb_width_window: int = Field(default=20, ge=5)

    @model_validator(mode="after")
    def _validate_exit_geometry(self):
        if self.atr_sl_floor >= self.atr_sl_cap:
            raise ValueError("atr_sl_floor must be < atr_sl_cap")
        if self.trail_give_back_points <= 0:
            raise ValueError("trail_give_back_points must be > 0")
        if self.be_enabled and self.trail_arm_points <= self.be_trigger_points:
            raise ValueError(
                "trail_arm_points must be > be_trigger_points "
                "(otherwise TRAIL pre-empts BE)"
            )
        if self.hard_sl_points <= 0:
            raise ValueError("hard_sl_points must be > 0")
        return self


@dataclass
class _PositionState:
    side: str
    entry_price: float
    entry_ts: datetime
    entry_ind: dict[str, float | None] = field(default_factory=dict)
    peak_pnl: float = 0.0
    sl_distance: float = 0.0
    breakeven_armed: bool = False


@dataclass
class _StratState:
    position: _PositionState | None = None
    cooldown_until: datetime | None = None
    last_long_ready: bool = False
    last_short_ready: bool = False


_STATE: dict[tuple[str, str], _StratState] = {}


def _state_for(name: str, symbol: str) -> _StratState:
    key = (name, symbol)
    st = _STATE.get(key)
    if st is None:
        st = _StratState()
        _STATE[key] = st
    return st


def _scalar(series: pd.Series | None, idx: int = -1) -> float | None:
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


def _short_entry_now(
    k_prev: float | None,
    d_prev: float | None,
    k_curr: float | None,
    d_curr: float | None,
    hist_curr: float | None,
    plus_prev: float | None,
    plus_curr: float | None,
    minus_prev: float | None,
    minus_curr: float | None,
    kd_short_ceiling: float,
) -> bool:
    if None in (
        k_prev, d_prev, k_curr, d_curr,
        hist_curr,
        plus_prev, plus_curr, minus_prev, minus_curr,
    ):
        return False
    if not (minus_curr > minus_prev and plus_curr < plus_prev):
        return False
    if not (k_prev < d_prev and k_curr < d_curr and k_prev > kd_short_ceiling):
        return False
    if not (hist_curr < 0.0):
        return False
    return True


def _clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _compute_sl_distance(
    atr_at_entry: float | None, p: TradeStrat1KAIParams
) -> float:
    if not p.use_atr_sl or atr_at_entry is None or not math.isfinite(atr_at_entry):
        return float(p.hard_sl_points)
    raw = float(atr_at_entry) * float(p.atr_sl_mult)
    return _clamp(raw, float(p.atr_sl_floor), float(p.atr_sl_cap))


def _bb_width_pct(bars: pd.DataFrame, window: int) -> float | None:
    """Bollinger-Band width over the last ``window`` closed bars, normalized
    by the current close. Returns ``None`` if insufficient history or
    degenerate values.

    Formula: (mid + 2σ − (mid − 2σ)) / close = 4σ / close.
    """
    if bars is None or len(bars) < window:
        return None
    closes = bars["close"].tail(window)
    sd = float(closes.std(ddof=0))
    close_now = float(closes.iloc[-1])
    if not math.isfinite(sd) or not math.isfinite(close_now) or close_now <= 0:
        return None
    return (4.0 * sd) / close_now


@register_strategy
class TradeStrat1KAI(Strategy):
    name: ClassVar[str] = "strat_1k_ai"
    display_name: ClassVar[str] = "1K策略AI版本"
    description: ClassVar[str] = (
        "1K策略 的出場重構版：保留同樣的進場條件，並啟用空單對稱進場；"
        "將原本的 TRAIL 拆成獨立的硬停損 (ATR 自適應)、保本、移動停損、"
        "時間截止與崩盤過濾。"
    )
    spec: ClassVar[dict[str, str]] = {
        "週期": "1 分鐘",
        "開倉時段": "08:45-13:45 / 15:00-05:00 (隔夜) (Asia/Taipei)",
        "進場": (
            "多單：+DI 上升、-DI 下降；KD 連兩 KS>DS 且首根 KS<70；MACD 直方>0；"
            "空單 (鏡像)：+DI 下降、-DI 上升；KD 連兩 KS<DS 且首根 KS>30；MACD 直方<0"
        ),
        "出場": (
            "停利依時段 (08:45-10:30 → 50；10:31-13:44 → 40；"
            "15:00-18:00 → 30；18:01-23:30 → 50；23:31-04:59 → 30)；"
            "硬停損 = ATR×1.8 (clamp 18–45 點，可關閉改用固定 30 點)；"
            "保本：浮動獲利 ≥15 點後若回到 0 即出場；"
            "移動停損：浮動獲利 ≥25 點後回撤 18 點出場；"
            "時間截止：持倉超過 30 分鐘且獲利不足 5 點即出場；"
            "崩盤過濾：對向 DI 跳升且 TR>2×ATR 強制出場"
        ),
        "冷卻": "出場後 5 分鐘 (300 秒)",
        "備註": (
            "與 strat_1k 共用 ToD 停利表；進場條件完全一致 (多單)，"
            "額外加上空單對稱版本；出場邏輯完整重構，所有閾值皆可從 "
            "/strategies/strat_1k_ai/params 介面調整"
        ),
    }
    resolutions: ClassVar[list[str]] = ["1m"]
    tick_resolutions: ClassVar[list[str]] = ["1m"]
    params_schema: ClassVar[type[BaseModel]] = TradeStrat1KAIParams
    indicator_specs: ClassVar[dict[str, dict]] = {
        "kd": {"kind": "kd", "params": {"period": 9, "k_smooth": 3, "d_smooth": 3}},
        "macd": {"kind": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
        "dmi": {"kind": "dmi", "params": {"period": 14}},
        "atr": {"kind": "atr", "params": {"period": 14}},
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
            "last_short_ready": st.last_short_ready,
            "position": (
                {
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "entry_ts": pos.entry_ts.isoformat(),
                    "peak_pnl": pos.peak_pnl,
                    "sl_distance": pos.sl_distance,
                    "breakeven_armed": pos.breakeven_armed,
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
        p: TradeStrat1KAIParams,
        *,
        symbol: str,
        resolution: str,
        bar_high: float | None = None,
        bar_low: float | None = None,
    ) -> Signal | None:
        pos = st.position
        if pos is not None and not in_entry_window(
            ts,
            get_settings().tz,
            day_open=_DAY_OPEN,
            day_close=_DAY_CLOSE,
            night_open=_NIGHT_OPEN,
            night_close=_NIGHT_CLOSE,
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
            exit_ind_snapshot = _snapshot_ind(kd, macd, dmi)
            st.position = None
            st.cooldown_until = ts + timedelta(seconds=p.cooldown_seconds)
            st.last_long_ready = False
            st.last_short_ready = False
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

        if st.cooldown_until is not None:
            if ts < st.cooldown_until:
                st.last_long_ready = False
                st.last_short_ready = False
                return None
            st.cooldown_until = None

        if st.position is not None:
            return self._manage_open_position(
                ts, price, bars, indicators, st, p,
                symbol=symbol, resolution=resolution,
                bar_high=bar_high, bar_low=bar_low,
            )

        return self._maybe_enter(
            ts, price, bars, indicators, st, p,
            symbol=symbol, resolution=resolution,
        )

    def _manage_open_position(
        self,
        ts: datetime,
        price: float,
        bars: pd.DataFrame,
        indicators: dict[str, pd.DataFrame],
        st: _StratState,
        p: TradeStrat1KAIParams,
        *,
        symbol: str,
        resolution: str,
        bar_high: float | None = None,
        bar_low: float | None = None,
    ) -> Signal | None:
        pos = st.position
        if pos is None:
            return None

        if pos.side == "LONG":
            pnl = price - pos.entry_price
        else:
            pnl = pos.entry_price - price

        pos.peak_pnl = max(pos.peak_pnl, pnl)

        # Bar-driven backtest path: detect SL / BE / TP / TRAIL crosses
        # using bar.high/low and fill at the exact target. Live tick path
        # bypasses this block (bar_high/low arrive as None).
        bar_exit = self._check_bar_exits(
            ts, pos, indicators, p,
            bar_high=bar_high, bar_low=bar_low,
            symbol=symbol, resolution=resolution, st=st,
        )
        if bar_exit is not None:
            return bar_exit

        kd = indicators.get("kd")
        macd = indicators.get("macd")
        dmi = indicators.get("dmi")
        atr = indicators.get("atr")
        snapshot = _snapshot_ind(kd, macd, dmi)
        if all(v is None for v in snapshot.values()) and pos.entry_ind:
            snapshot = dict(pos.entry_ind)

        # 1) Crash regime override — adverse DI thrust AND TR burst.
        if p.crash_regime_exit:
            plus = _scalar(dmi["plus_di"]) if dmi is not None else None
            minus = _scalar(dmi["minus_di"]) if dmi is not None else None
            if plus is not None and minus is not None:
                adverse = (minus - plus) if pos.side == "LONG" else (plus - minus)
                # Compute current TR from last bar OHLC + previous close.
                # NaN-safe: only compare TR against the *previous* ATR so the
                # current burst can't smooth itself out of being detected.
                tr_value: float | None = None
                try:
                    h = float(bars["high"].iloc[-1])
                    lo = float(bars["low"].iloc[-1])
                    prev_close_raw = (
                        bars["close"].iloc[-2] if len(bars) >= 2 else None
                    )
                    if prev_close_raw is None:
                        prev_close = None
                    else:
                        try:
                            prev_close = float(prev_close_raw)
                            if math.isnan(prev_close):
                                prev_close = None
                        except (TypeError, ValueError):
                            prev_close = None
                    if (
                        math.isnan(h)
                        or math.isnan(lo)
                        or prev_close is None
                    ):
                        tr_value = h - lo if not (math.isnan(h) or math.isnan(lo)) else None
                    else:
                        tr_value = max(
                            h - lo,
                            abs(h - prev_close),
                            abs(lo - prev_close),
                        )
                except (IndexError, ValueError, KeyError, TypeError):
                    tr_value = None
                # Use atr[-2] so the current bar's burst isn't already
                # smoothed into the comparison baseline.
                atr_now: float | None = None
                if atr is not None:
                    atr_now = _scalar(atr["atr"], idx=-2)
                    if atr_now is None:
                        atr_now = _scalar(atr["atr"])
                if (
                    adverse > p.crash_di_spread_threshold
                    and tr_value is not None
                    and atr_now is not None
                    and atr_now > 0
                    and tr_value > atr_now * p.crash_atr_burst_mult
                ):
                    return self._close_position(
                        ts, price, "CRASH", pnl,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )

        # 2) TP — unchanged from strat_1k ToD table.
        tp, _ = _exit_params_for(ts, get_settings().tz)
        if pnl >= tp:
            return self._close_position(
                ts, price, "TP", pnl,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )

        # 3) Hard SL — ATR-scaled or fixed.
        if pnl <= -pos.sl_distance:
            return self._close_position(
                ts, price, "SL", pnl,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )

        # 4) Break-even — arm after enough peak profit, exit when pnl ≤ 0.
        if p.be_enabled:
            if pos.peak_pnl >= p.be_trigger_points:
                pos.breakeven_armed = True
            if pos.breakeven_armed and pnl <= 0:
                return self._close_position(
                    ts, price, "BE", pnl,
                    st=st, p=p, exit_ind=snapshot,
                    symbol=symbol, resolution=resolution,
                )

        # 5) Trail — arms only after profit threshold reached. Per-side
        # enable flags route LONG / SHORT positions through different
        # trail policies (or skip TRAIL entirely on one side).
        trail_side_enabled = (
            p.trail_long_enabled if pos.side == "LONG" else p.trail_short_enabled
        )
        if (
            trail_side_enabled
            and pos.peak_pnl >= p.trail_arm_points
            and pnl <= pos.peak_pnl - p.trail_give_back_points
        ):
            return self._close_position(
                ts, price, "TRAIL", pnl,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )

        # 6) Time cutoff — scratch stale trades.
        age_minutes = (ts - pos.entry_ts).total_seconds() / 60.0
        if (
            p.time_cutoff_minutes > 0
            and age_minutes >= p.time_cutoff_minutes
            and pnl < p.time_cutoff_min_pnl
        ):
            return self._close_position(
                ts, price, "TIME", pnl,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )

        return None

    def _check_bar_exits(
        self,
        ts: datetime,
        pos: _PositionState,
        indicators: dict[str, pd.DataFrame],
        p: TradeStrat1KAIParams,
        *,
        bar_high: float | None,
        bar_low: float | None,
        symbol: str,
        resolution: str,
        st: _StratState,
    ) -> Signal | None:
        """Bar-driven exit check using intra-bar extremes.

        Pessimistic ordering: SL → BE → TP → TRAIL. Lets backtest fills at
        the exact target price when the bar's high/low crossed it, matching
        the tick-driven live behavior. Tick-driven callers pass
        ``bar_high=bar_low=None`` and this returns ``None`` immediately.
        """
        if bar_high is None or bar_low is None:
            return None

        kd = indicators.get("kd")
        macd = indicators.get("macd")
        dmi = indicators.get("dmi")
        snapshot = _snapshot_ind(kd, macd, dmi)
        if all(v is None for v in snapshot.values()) and pos.entry_ind:
            snapshot = dict(pos.entry_ind)

        tp, _ = _exit_params_for(ts, get_settings().tz)

        if pos.side == "LONG":
            # Effective peak considering this bar's high.
            effective_peak = max(pos.peak_pnl, bar_high - pos.entry_price)

            # SL — pessimistic first, since a deep low typically arrives
            # before any peak update could have armed BE / TRAIL.
            sl_target = pos.entry_price - pos.sl_distance
            if bar_low <= sl_target:
                return self._close_position(
                    ts, sl_target, "SL", -pos.sl_distance,
                    st=st, p=p, exit_ind=snapshot,
                    symbol=symbol, resolution=resolution,
                )

            # BE — armed when peak crosses trigger; exits at entry on dip.
            if p.be_enabled:
                if effective_peak >= p.be_trigger_points:
                    pos.breakeven_armed = True
                if pos.breakeven_armed and bar_low <= pos.entry_price:
                    return self._close_position(
                        ts, pos.entry_price, "BE", 0.0,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )

            # TP — exit at target when high reaches it.
            tp_target = pos.entry_price + tp
            if bar_high >= tp_target:
                pos.peak_pnl = effective_peak
                return self._close_position(
                    ts, tp_target, "TP", tp,
                    st=st, p=p, exit_ind=snapshot,
                    symbol=symbol, resolution=resolution,
                )

            # TRAIL — give-back from effective peak.
            if p.trail_long_enabled and effective_peak >= p.trail_arm_points:
                trail_pnl = effective_peak - p.trail_give_back_points
                trail_target = pos.entry_price + trail_pnl
                if bar_low <= trail_target:
                    pos.peak_pnl = effective_peak
                    return self._close_position(
                        ts, trail_target, "TRAIL", trail_pnl,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )

            pos.peak_pnl = effective_peak
            return None

        # SHORT mirror.
        effective_peak = max(pos.peak_pnl, pos.entry_price - bar_low)

        sl_target = pos.entry_price + pos.sl_distance
        if bar_high >= sl_target:
            return self._close_position(
                ts, sl_target, "SL", -pos.sl_distance,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )

        if p.be_enabled:
            if effective_peak >= p.be_trigger_points:
                pos.breakeven_armed = True
            if pos.breakeven_armed and bar_high >= pos.entry_price:
                return self._close_position(
                    ts, pos.entry_price, "BE", 0.0,
                    st=st, p=p, exit_ind=snapshot,
                    symbol=symbol, resolution=resolution,
                )

        tp_target = pos.entry_price - tp
        if bar_low <= tp_target:
            pos.peak_pnl = effective_peak
            return self._close_position(
                ts, tp_target, "TP", tp,
                st=st, p=p, exit_ind=snapshot,
                symbol=symbol, resolution=resolution,
            )

        if p.trail_short_enabled and effective_peak >= p.trail_arm_points:
            trail_pnl = effective_peak - p.trail_give_back_points
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

    def _maybe_enter(
        self,
        ts: datetime,
        price: float,
        bars: pd.DataFrame,
        indicators: dict[str, pd.DataFrame],
        st: _StratState,
        p: TradeStrat1KAIParams,
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
            st.last_short_ready = False
            return None

        # Pre-entry micro-vol filter: when 1m BB width is wide (last N closed
        # bars), the strategy gets whipsawed into SL more often. Skip silently
        # — do NOT touch the latch flags so the next quieter tick can fire.
        if p.bb_width_filter_enabled:
            bb_pct = _bb_width_pct(bars, p.bb_width_window)
            if bb_pct is not None and bb_pct >= p.bb_width_max_pct:
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

        short_now = False
        short_rising = False
        if p.enable_short:
            short_now = _short_entry_now(
                k_prev, d_prev, k_curr, d_curr,
                hist_curr,
                plus_prev, plus_curr, minus_prev, minus_curr,
                kd_short_ceiling=p.kd_short_ceiling,
            )
            short_rising = short_now and not st.last_short_ready
            st.last_short_ready = short_now
        else:
            st.last_short_ready = False

        # Deterministic tie-break: LONG wins if both latches fire on the
        # same evaluation. In practice the gates are mutually exclusive on
        # well-formed data — KD and MACD-hist signs cannot both hold.
        if long_rising:
            return self._open_position(
                ts, price, side="LONG",
                st=st, p=p, kd=kd, macd=macd, dmi=dmi,
                atr=indicators.get("atr"),
                symbol=symbol, resolution=resolution,
            )
        if short_rising:
            return self._open_position(
                ts, price, side="SHORT",
                st=st, p=p, kd=kd, macd=macd, dmi=dmi,
                atr=indicators.get("atr"),
                symbol=symbol, resolution=resolution,
            )
        return None

    def _open_position(
        self,
        ts: datetime,
        price: float,
        *,
        side: str,
        st: _StratState,
        p: TradeStrat1KAIParams,
        kd: pd.DataFrame | None,
        macd: pd.DataFrame | None,
        dmi: pd.DataFrame | None,
        atr: pd.DataFrame | None,
        symbol: str,
        resolution: str,
    ) -> Signal:
        snap = _snapshot_ind(kd, macd, dmi)
        atr_at_entry = _scalar(atr["atr"]) if atr is not None else None
        sl_distance = _compute_sl_distance(atr_at_entry, p)
        st.position = _PositionState(
            side=side, entry_price=price, entry_ts=ts,
            entry_ind=dict(snap), peak_pnl=0.0,
            sl_distance=sl_distance, breakeven_armed=False,
        )
        tp_at_entry, _ = _exit_params_for(ts, get_settings().tz)
        nan = float("nan")
        k_disp = snap.get("k") if snap.get("k") is not None else nan
        d_disp = snap.get("d") if snap.get("d") is not None else nan
        macd_disp = snap.get("macd") if snap.get("macd") is not None else nan
        plus_disp = snap.get("plus_di") if snap.get("plus_di") is not None else nan
        minus_disp = snap.get("minus_di") if snap.get("minus_di") is not None else nan
        di_disp = minus_disp if side == "SHORT" else plus_disp
        di_tag = "-DI" if side == "SHORT" else "+DI"
        return Signal(
            ts=ts,
            symbol=symbol,
            resolution=resolution,
            strategy=self.name,
            side=side,
            price=price,
            reason=(
                f"entry {side}: K={k_disp:.1f} D={d_disp:.1f} "
                f"MACD={macd_disp:.2f} {di_tag}={di_disp:.1f}"
            ),
            payload={
                "entry_ind": snap,
                "tp_points": tp_at_entry,
                "sl_points": sl_distance,
                "atr_at_entry": atr_at_entry,
                "be_trigger_points": p.be_trigger_points,
                "trail_arm_points": p.trail_arm_points,
                "trail_give_back_points": p.trail_give_back_points,
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
        p: TradeStrat1KAIParams,
        exit_ind: dict[str, float | None] | None = None,
        symbol: str,
        resolution: str,
    ) -> Signal:
        pos = st.position
        st.position = None
        st.cooldown_until = ts + timedelta(seconds=p.cooldown_seconds)
        st.last_long_ready = False
        st.last_short_ready = False
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
