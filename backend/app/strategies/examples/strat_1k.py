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
  4. 5m direction-positive: on the most recent CLOSED 5m bar,
     ``plus_di > minus_di`` strictly. Default mode ``"di_positive"`` of
     ``require_5m_alignment``. Shipped 2026-05-21 after a 182d walk-forward
     + codex audit found the unfiltered 3-gate stack net-losing over
     6 months (PF 0.987, PnL -613). With this 4th gate live: PF 1.080,
     PnL +1230, max DD 958 (-4.7×). Flip to ``None`` to revert to the
     unfiltered 3-gate stack for ablation studies. Other alignment modes
     (macd_hist / above_ema20 / inverted variants) are analyst-only.
  5. (OPTIONAL — DISABLED BY DEFAULT) 5m V-rebound (when
     ``vrebound_enabled=True``): the last
     ``vrebound_red + vrebound_green`` CLOSED 5-minute bars form
     ``[red]*vrebound_red + [green]*vrebound_green`` by ``close vs open``
     (red = ``close<open``, green = ``close>open``). Defaults 2 red then
     2 green. Doji (``close==open``) blocks. Insufficient closed history
     blocks. The trim guards the backtest path where ``df.loc[:ts]`` on
     the 5m aux series can include an in-progress bucket; rows whose
     ``bucket+5min`` exceed the evaluation ``ts`` are dropped before the
     pattern check.

     Disabled in 2026-05-21: 30-day BT showed adverse selection
     (filter reduced WR vs 4-gate baseline). V-rebound is a
     mean-reversion primitive that conflicts with the bullish-momentum
     4-gate stack. Kept as a tunable param for future experiments;
     do not re-enable without a walk-forward validation.

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
from typing import ClassVar, Literal

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

_VREBOUND_RES_DELTA = timedelta(minutes=5)
_FIVE_MIN_RES_DELTA = timedelta(minutes=5)


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

    # V-rebound 5m gate. The aux resolution is fixed at 5m in
    # ``aux_indicator_specs`` (class-level; cannot read instance params),
    # so only the counts and enabled flag are tunable here.
    vrebound_enabled: bool = False
    vrebound_red: int = Field(default=2, ge=1, le=10)
    vrebound_green: int = Field(default=2, ge=1, le=10)

    # Gate-ablation knob for offline studies (see subagent-1 gate ablation
    # analysis, 2026-05-21). When set, skip the named primary entry gate
    # while leaving the other two + the entry-window gate intact. ``None``
    # preserves the live 3-gate AND stack — DO NOT flip this to a non-None
    # default; this is an analyst toggle, not a strategy variant.
    ablate_gate: Literal["dmi", "kd", "macd"] | None = None

    # 5m higher-timeframe alignment gate (off-by-default experiment, see
    # plan `smooth-sniffing-meadow.md`). When set, an extra entry gate
    # consults the most-recent CLOSED 5m bar and blocks LONG entry if the
    # named alignment condition fails. Modes:
    #   - "di_positive"        : block if 5m +DI ≤ -DI    (DEFAULT, shipped 2026-05-21)
    #   - "macd_hist"          : block if 5m MACD hist ≤ 0
    #   - "above_ema20"        : block if 5m close < 5m EMA20
    #   - "macd_hist_negative" : block if 5m MACD hist > 0  (inverted, analyst-only)
    #   - "di_negative"        : block if 5m +DI > -DI      (inverted, analyst-only)
    #   - "below_ema20"        : block if 5m close > 5m EMA20 (inverted, analyst-only)
    # Default ``"di_positive"`` is the shipping configuration from the 2026-05-21
    # 182d walk-forward + codex audit. Backtest over 2025-11-19 → 2026-05-21:
    # baseline 3151 trades / PF 0.987 / PnL -613 / DD 4559  →  filter active:
    # 1073 trades / PF 1.080 / PnL +1230 / DD 958. Flip to ``None`` to revert
    # to the unfiltered 3-gate stack (e.g. for ablation studies). The aux 5m
    # indicator specs are always declared (cheap precompute) so the framework
    # wires them regardless of which mode is active.
    require_5m_alignment: (
        Literal[
            "macd_hist",
            "di_positive",
            "above_ema20",
            "macd_hist_negative",
            "di_negative",
            "below_ema20",
        ]
        | None
    ) = "di_positive"


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
    ablate_gate: Literal["dmi", "kd", "macd"] | None = None,
) -> bool:
    """Evaluate the 1-minute primary entry gates (all AND-ed).

    Returns True iff every (non-ablated) gate holds:
      DMI rising — ``plus_curr > plus_prev`` AND ``minus_curr < minus_prev``.
      KD         — ``k_prev > d_prev`` AND ``k_curr > d_curr`` AND
                   ``k_prev < kd_long_floor``.
      MACD       — ``hist_curr > 0`` (strict).

    ``ablate_gate`` (default ``None``) skips one gate's check. The
    associated indicator inputs may legitimately be ``None`` under
    ablation, so the None-guard at the top is loosened per-gate. The
    entry-window gate is structural and never ablatable.
    """
    if ablate_gate != "dmi" and None in (
        plus_prev, plus_curr, minus_prev, minus_curr,
    ):
        return False
    if ablate_gate != "kd" and None in (k_prev, d_prev, k_curr, d_curr):
        return False
    if ablate_gate != "macd" and hist_curr is None:
        return False
    if ablate_gate != "dmi" and not (
        plus_curr > plus_prev and minus_curr < minus_prev
    ):
        return False
    if ablate_gate != "kd" and not (
        k_prev > d_prev and k_curr > d_curr and k_prev < kd_long_floor
    ):
        return False
    if ablate_gate != "macd" and not (hist_curr > 0.0):
        return False
    return True


def _vrebound_ok(
    direction: pd.Series | None,
    ts: datetime,
    red: int,
    green: int,
) -> bool:
    """True iff the last ``red+green`` CLOSED 5m direction values are
    exactly ``[-1]*red + [+1]*green``.

    Defensive close-trim: in backtest, ``df.loc[:ts]`` on a 5m aux series
    can surface a bucket whose end exceeds the 1m evaluation ``ts`` (e.g.
    ``ts=09:01`` with a row at ``bucket=09:00`` that closes at 09:05).
    Drop any row whose ``index + 5min > ts`` before counting. Live path
    is already closed-only via the ``load_bars`` cutoff, so the trim is a
    no-op there.

    Doji (``0``) blocks. Insufficient length after trim blocks. ``None``
    or empty input blocks.
    """
    if direction is None or direction.empty:
        return False
    closed = direction[direction.index + _VREBOUND_RES_DELTA <= ts]
    need = red + green
    if len(closed) < need:
        return False
    tail = closed.iloc[-need:].astype(int).tolist()
    expected = [-1] * red + [1] * green
    return tail == expected


def _five_min_aligned(
    mode: str,
    ts: datetime,
    aux: dict[str, pd.DataFrame],
) -> bool:
    """True iff the last fully-CLOSED 5m bar passes the alignment mode.

    Positive modes (gate passes when the named indicator is bullish):
      - ``"macd_hist"``   — 5m MACD hist > 0.
      - ``"di_positive"`` — 5m +DI > -DI.
      - ``"above_ema20"`` — 5m close > 5m EMA20.

    Inverted modes (gate passes when the named indicator is bearish — used
    for the inverted-direction study `smooth-sniffing-meadow.md` Study 5;
    the predicate is the strict negation of the positive mode, so a row
    exactly on the boundary — hist=0, +DI=-DI, close=EMA20 — passes the
    inverted gate but fails the positive one):
      - ``"macd_hist_negative"`` — 5m MACD hist ≤ 0.
      - ``"di_negative"``        — 5m +DI ≤ -DI.
      - ``"below_ema20"``        — 5m close ≤ 5m EMA20.

    Close-trim: only rows where ``index + 5min <= ts`` are considered,
    mirroring the V-rebound defensive trim. Insufficient closed history
    (no rows after trim), missing aux DataFrame, missing required
    columns, or None/NaN values all block.
    """
    if mode in ("macd_hist", "macd_hist_negative"):
        df = aux.get("macd_5m")
        required = ("hist",)
    elif mode in ("di_positive", "di_negative"):
        df = aux.get("dmi_5m")
        required = ("plus_di", "minus_di")
    elif mode in ("above_ema20", "below_ema20"):
        df = aux.get("ma_5m")
        required = ("close", "ma")
    else:
        return False

    if df is None or df.empty:
        return False
    if any(col not in df.columns for col in required):
        return False

    closed = df[df.index + _FIVE_MIN_RES_DELTA <= ts]
    if closed.empty:
        return False

    if mode in ("macd_hist", "macd_hist_negative"):
        hist = _scalar(closed["hist"])
        if hist is None:
            return False
        return hist > 0.0 if mode == "macd_hist" else hist <= 0.0
    if mode in ("di_positive", "di_negative"):
        plus = _scalar(closed["plus_di"])
        minus = _scalar(closed["minus_di"])
        if plus is None or minus is None:
            return False
        return plus > minus if mode == "di_positive" else plus <= minus
    if mode in ("above_ema20", "below_ema20"):
        close = _scalar(closed["close"])
        ma = _scalar(closed["ma"])
        if close is None or ma is None:
            return False
        return close > ma if mode == "above_ema20" else close <= ma
    return False


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
        "1m +DI 上升 -DI 下降、1m KD 連兩 KS>DS 且首根 KS<70、1m MACD 直方>0、"
        "5 分鐘 +DI > -DI；"
        "出場：依時段獲利 30/40/50 點；移動停損 40 點。"
        " (V 形反轉閘預設關閉；5 分鐘對齊閘可透過 require_5m_alignment 切換模式或設 None 關閉。)"
    )
    spec: ClassVar[dict[str, str]] = {
        "週期": "1 分鐘",
        "開倉時段": "08:45-13:45 / 15:00-05:00 (隔夜) (Asia/Taipei)",
        "進場": (
            "1 分鐘 +DI 上升、-DI 下降；"
            "1 分鐘 KD 連兩 KS>DS 且第一根 KS<70；"
            "1 分鐘 MACD 直方圖>0；"
            "5 分鐘 +DI > -DI (最後一根收盤 5m) (全部成立)。"
            "選用：require_5m_alignment 可切換為 macd_hist / above_ema20 或 None 關閉；"
            "vrebound_enabled=True 另可開啟 5 分鐘 V 形反轉閘"
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
    aux_indicator_specs: ClassVar[dict[str, dict]] = {
        "vrebound_5m": {
            "kind": "candle_direction",
            "params": {},
            "resolution": "5m",
        },
        # 5m alignment-gate aux indicators. Always declared so the
        # framework precomputes them; ``_five_min_aligned`` only reads
        # the one needed when ``require_5m_alignment`` is non-None.
        "macd_5m": {
            "kind": "macd",
            "params": {"fast": 12, "slow": 26, "signal": 9},
            "resolution": "5m",
        },
        "dmi_5m": {
            "kind": "dmi",
            "params": {"period": 14},
            "resolution": "5m",
        },
        "ma_5m": {
            "kind": "ma",
            "params": {"period": 20, "kind": "ema"},
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
        # Surface intra-bar extremes so the exit path can detect a TP
        # crossing that occurred between open and close — without these,
        # backtest fills TP at bar close while live fills at the exact
        # tick that crossed the target.
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
        p: TradeStrat1KParams,
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

        if st.cooldown_until is not None:
            if ts < st.cooldown_until:
                st.last_long_ready = False
                return None
            st.cooldown_until = None

        if st.position is not None:
            return self._manage_open_position(
                ts, price, indicators, st, p,
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
        indicators: dict[str, pd.DataFrame],
        st: _StratState,
        p: TradeStrat1KParams,
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

        tp, trail = _exit_params_for(ts, get_settings().tz)

        # Intra-bar TP / TRAIL check (bar-driven path only). Lets backtest
        # fill at the exact target price when the bar's high/low crossed
        # it — same outcome the tick-driven live path produces.
        if bar_high is not None and bar_low is not None:
            if pos.side == "LONG":
                tp_target = pos.entry_price + tp
                if bar_high >= tp_target:
                    return self._close_position(
                        ts, tp_target, "TP", tp,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )
                # Peak may have moved up intra-bar before the trail check.
                effective_peak = max(pos.peak_pnl, bar_high - pos.entry_price)
                trail_pnl = effective_peak - trail
                trail_target = pos.entry_price + trail_pnl
                if bar_low <= trail_target:
                    return self._close_position(
                        ts, trail_target, "TRAIL", trail_pnl,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )
                pos.peak_pnl = effective_peak
            else:  # SHORT — defensive; strat_1k is LONG-only by spec.
                tp_target = pos.entry_price - tp
                if bar_low <= tp_target:
                    return self._close_position(
                        ts, tp_target, "TP", tp,
                        st=st, p=p, exit_ind=snapshot,
                        symbol=symbol, resolution=resolution,
                    )
                effective_peak = max(pos.peak_pnl, pos.entry_price - bar_low)
                trail_pnl = effective_peak - trail
                trail_target = pos.entry_price - trail_pnl
                if bar_high >= trail_target:
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
            ablate_gate=p.ablate_gate,
        )

        # 5m V-rebound confirmation. Treated as a 5th AND gate so the
        # rising-edge latch reflects the full conjunction — a 4-gate
        # match that fails V-rebound does not arm the latch, and the
        # first tick where V flips ok with the 4 gates still holding
        # fires as a fresh rising edge.
        if long_now and p.vrebound_enabled:
            aux = indicators.get("vrebound_5m")
            direction = (
                aux["direction"]
                if aux is not None and not aux.empty and "direction" in aux.columns
                else None
            )
            if not _vrebound_ok(direction, ts, p.vrebound_red, p.vrebound_green):
                long_now = False

        # 5m higher-timeframe alignment gate (off-by-default experiment).
        # Same rising-edge semantics as V-rebound: a 3-gate match that
        # fails 5m alignment does not arm the latch.
        if long_now and p.require_5m_alignment is not None:
            if not _five_min_aligned(p.require_5m_alignment, ts, indicators):
                long_now = False

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
