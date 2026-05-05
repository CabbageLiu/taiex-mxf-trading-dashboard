"""TAIEX 1-minute Strategy (1K策略).

Single-resolution LONG-only strategy. Entry fires the moment the gates
align intra-bar (tick-driven); exits (TP / SL / TRAIL) fire the moment the
tick price crosses the threshold; DI_JUMP_1M still evaluates against
closed-bar -DI deltas. Both ``on_bar`` (back-compat / backtest path) and
``on_tick`` route through the same ``_evaluate`` helper.

Entry gates (all must hold; LONG only):
  0. Entry window: Asia/Taipei time inside [09:10, 11:15) ∪ [15:00, 24:00).
  1. KD (1m): ``k[-2] > d[-2]`` AND ``k[-1] > d[-1]`` AND ``k[-2] < 65``.
  2. MACD histogram (1m): ``hist[-1] > hist[-2]`` (rising; allows
     ``hist[-2] >= 0`` cases as long as second value is strictly larger).
  3. DMI (1m): ``plus[-2] > minus[-2]`` AND ``plus[-1] > minus[-1]`` AND
     ``minus[-1] < minus[-2]``.
  4. 3m KD confirmation: ``k_3m[-2] > d_3m[-2]`` AND
     ``k_3m[-1] > d_3m[-1]`` AND ``k_3m[-2] < 65``. Cold/empty/stale
     beyond 9 minutes (3 × 3m) all block.
  5. 3m MACD confirmation: ``hist_3m[-1] > hist_3m[-2]`` (rising). Same
     9-minute staleness guard.
  6. 3m DMI confirmation: ``plus_3m[-2] > minus_3m[-2]`` AND
     ``plus_3m[-1] > minus_3m[-1]`` AND ``minus_3m[-1] < minus_3m[-2]``.
     Same 9-minute staleness guard.

Note: the spec does NOT include an MA120 trend gate for 1K. The previous
revision required ``close > MA120 AND MA120 rising``; the gate is removed.

Exit priority (per tick or bar close, first match wins):
  TP          — pnl ≥ 50
  SL          — pnl ≤ −40
  TRAIL       — pnl ≤ peak_pnl − 50 (peak tracked from entry, starts at 0)
  DI_JUMP_1M  — minus_di[-1] − minus_di[-2] > 10 (strict ``>``, closed bars)

Exits ignore the entry-window gate AND the 3m aux gates — open positions
must remain closeable any time, including the 11:15–15:00 no-entry stretch
and the overnight 00:00–05:00 stretch.

Cooldown: 300 seconds after EXIT (time-based, not bar-counted). While
``ts < cooldown_until`` evaluation returns None and resets
``last_long_ready`` so the rising-edge latch re-arms once cooldown clears.
The window gate behaves the same way: when it blocks, the latch resets so
the first aligned tick after the window reopens fires cleanly as a fresh
rising edge.

Fill convention: tick-driven (not bar close). ``Signal.ts`` carries the
raw tick timestamp, so ``signals.ts`` / ``trades.entry_ts`` /
``trades.exit_ts`` reflect actual fill time.
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
_DAY_CLOSE = time(11, 15)
_NIGHT_OPEN = time(15, 0)
_AUX_STALENESS = timedelta(minutes=9)  # 3 × 3m


class TradeStrat1KParams(BaseModel):
    enable_short: bool = False

    kd_period: int = 9
    kd_k_smooth: int = 3
    kd_d_smooth: int = 3
    kd_long_floor: float = 65.0  # first 1m K must be below this

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    dmi_period: int = 14

    kd_3m_floor: float = 65.0  # first 3m K must be below this to confirm

    tp_points: float = 50.0
    sl_points: float = 40.0
    trail_points: float = 50.0
    di_jump_points: float = 10.0

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
    hist_prev: float | None,
    hist_curr: float | None,
    plus_prev: float | None,
    plus_curr: float | None,
    minus_prev: float | None,
    minus_curr: float | None,
    kd_long_floor: float,
) -> bool:
    """Evaluate the three primary 1m entry gates (KD / MACD / DMI).

    Spec drops the MA120 trend gate (close>MA + MA rising) at 1K — the
    helper signature reflects that. The MACD gate is "rising" semantics:
    ``hist_curr > hist_prev`` (both non-None), permitting both
    ``hist_prev < 0`` and ``hist_prev >= 0`` cases as long as the second
    histogram strictly exceeds the first.
    """
    if None in (
        k_prev, d_prev, k_curr, d_curr,
        hist_prev, hist_curr,
        plus_prev, plus_curr, minus_prev, minus_curr,
    ):
        return False
    gate_kd = k_prev > d_prev and k_curr > d_curr and k_prev < kd_long_floor
    gate_macd = hist_curr > hist_prev
    gate_dmi = (
        plus_prev > minus_prev
        and plus_curr > minus_curr
        and minus_curr < minus_prev
    )
    return gate_kd and gate_macd and gate_dmi


def _aux_fresh(
    df: pd.DataFrame | None,
    ts: datetime,
    max_age: timedelta = _AUX_STALENESS,
) -> bool:
    """Common staleness check for 3m aux frames.

    Returns True when ``df`` exists, has at least one row, and the most
    recent index entry is younger than ``max_age`` vs ``ts``.
    """
    if df is None or len(df) == 0:
        return False
    last_idx = df.index[-1]
    if isinstance(last_idx, pd.Timestamp):
        last_dt = last_idx.to_pydatetime()
    else:
        last_dt = last_idx
    if last_dt.tzinfo is None and ts.tzinfo is not None:
        last_dt = last_dt.replace(tzinfo=ts.tzinfo)
    elif last_dt.tzinfo is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=last_dt.tzinfo)
    return (ts - last_dt) < max_age


def _kd_3m_ok(
    kd_3m: pd.DataFrame | None,
    ts: datetime,
    floor: float,
) -> bool:
    """Auxiliary 3-minute KD confirmation gate.

    Block when the aux frame is missing/empty/stale (>=9min) or any of the
    four K/D scalars is None/NaN, the two K/D pairs do not show K>D, or
    the FIRST K is not strictly below ``floor`` (default 65). This matches
    spec wording "first KS<65".
    """
    if not _aux_fresh(kd_3m, ts):
        return False
    if "k" not in kd_3m.columns or "d" not in kd_3m.columns:
        return False
    k_prev = _scalar(kd_3m["k"], idx=-2)
    d_prev = _scalar(kd_3m["d"], idx=-2)
    k_curr = _scalar(kd_3m["k"], idx=-1)
    d_curr = _scalar(kd_3m["d"], idx=-1)
    if None in (k_prev, d_prev, k_curr, d_curr):
        return False
    return k_prev > d_prev and k_curr > d_curr and k_prev < floor


def _macd_3m_rising(
    macd_3m: pd.DataFrame | None,
    ts: datetime,
) -> bool:
    """Auxiliary 3-minute MACD-histogram rising gate.

    Block when missing/empty/stale (>=9min) or either of the last two
    histogram scalars is None/NaN or the second histogram is not strictly
    larger than the first. Matches spec wording "second MACD histogram >
    first MACD histogram" — no zero-cross requirement.
    """
    if not _aux_fresh(macd_3m, ts):
        return False
    if "hist" not in macd_3m.columns:
        return False
    hist_prev = _scalar(macd_3m["hist"], idx=-2)
    hist_curr = _scalar(macd_3m["hist"], idx=-1)
    if hist_prev is None or hist_curr is None:
        return False
    return hist_curr > hist_prev


def _dmi_3m_ok(
    dmi_3m: pd.DataFrame | None,
    ts: datetime,
) -> bool:
    """Auxiliary 3-minute DMI confirmation gate.

    Block when missing/empty/stale (>=9min) or any +DI / -DI scalar is
    None/NaN, the two +DI / -DI pairs do not show +DI > -DI, or the second
    -DI is not strictly less than the first -DI.
    """
    if not _aux_fresh(dmi_3m, ts):
        return False
    if "plus_di" not in dmi_3m.columns or "minus_di" not in dmi_3m.columns:
        return False
    plus_prev = _scalar(dmi_3m["plus_di"], idx=-2)
    plus_curr = _scalar(dmi_3m["plus_di"], idx=-1)
    minus_prev = _scalar(dmi_3m["minus_di"], idx=-2)
    minus_curr = _scalar(dmi_3m["minus_di"], idx=-1)
    if None in (plus_prev, plus_curr, minus_prev, minus_curr):
        return False
    return (
        plus_prev > minus_prev
        and plus_curr > minus_curr
        and minus_curr < minus_prev
    )


@register_strategy
class TradeStrat1K(Strategy):
    name: ClassVar[str] = "strat_1k"
    display_name: ClassVar[str] = "1K策略"
    description: ClassVar[str] = (
        "1 分鐘多單策略；開倉時段 09:10-11:15 / 15:00-24:00；進場："
        "1m KD 連兩 KS>DS 且首根 KS<65、1m MACD 直方上升 (第二根>第一根)、"
        "+DI>-DI 且 -DI 縮、3m KD 連兩 KS>DS 且首根 KS<65、3m MACD 直方上升、"
        "3m +DI>-DI 且 -DI 縮；"
        "出場：TP 50 / SL −40 / 移動停損 50 / 1 分鐘 -DI 跳升 (>10 點)。"
    )
    spec: ClassVar[dict[str, str]] = {
        "週期": "1 分鐘 (3 分鐘輔助)",
        "開倉時段": "09:10-11:15 / 15:00-24:00 (Asia/Taipei)",
        "進場": (
            "1 分鐘 KD 連兩根 KS>DS 且第一根 KS<65；"
            "1 分鐘 MACD 直方圖第二根>第一根；"
            "1 分鐘 +DI>-DI 連兩根且第二根 -DI 縮；"
            "3 分鐘 KD 連兩根 KS>DS 且第一根 KS<65；"
            "3 分鐘 MACD 直方圖第二根>第一根；"
            "3 分鐘 +DI>-DI 連兩根且第二根 -DI 縮"
        ),
        "出場": (
            "獲利 50 點 / 虧損 40 點 / 移動停損 50 點 / "
            "1 分鐘 -DI 跳升 (>10 點)"
        ),
        "冷卻": "出場後 5 分鐘 (300 秒)",
        "備註": (
            "僅多單；訊號逐筆即時觸發；出場不受開倉時段與 3m 輔助條件限制"
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
        "kd_3m": {
            "kind": "kd",
            "params": {"period": 9, "k_smooth": 3, "d_smooth": 3},
            "resolution": "3m",
        },
        "macd_3m": {
            "kind": "macd",
            "params": {"fast": 12, "slow": 26, "signal": 9},
            "resolution": "3m",
        },
        "dmi_3m": {
            "kind": "dmi",
            "params": {"period": 14},
            "resolution": "3m",
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
        p: TradeStrat1KParams,
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

        # 2. Manage open position.
        if st.position is not None:
            return self._manage_open_position(
                ts, price, indicators, st, p,
                symbol=symbol, resolution=resolution,
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

        # Priority order: TP → SL → TRAIL → DI_JUMP_1M.
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

        # DI_JUMP: -DI just jumped > di_jump_points across the last two
        # closed bars (closed-bar indicator only updates at bucket roll).
        if dmi is not None:
            minus_prev = _scalar(dmi["minus_di"], idx=-2)
            minus_curr = _scalar(dmi["minus_di"], idx=-1)
            if (
                minus_prev is not None
                and minus_curr is not None
                and (minus_curr - minus_prev) > p.di_jump_points
            ):
                return self._close_position(
                    ts, price, "DI_JUMP_1M", pnl,
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
        # 0a. Entry-window gate. When closed, reset the rising-edge latch so
        # that pre-aligned gates fire as a fresh rising edge on the first
        # tick after the window reopens.
        if not in_entry_window(
            ts,
            get_settings().tz,
            day_open=_DAY_OPEN,
            day_close=_DAY_CLOSE,
            night_open=_NIGHT_OPEN,
        ):
            st.last_long_ready = False
            return None

        # 0b. 3m KD / MACD / DMI confirmation gates (entry-only; exits ignore
        # them). Each helper handles missing/empty/stale (>=9min) → block.
        # Latch reset on every block path so a recovering aux gate is
        # detected as a fresh rising edge, not a phantom one carried over
        # from a previous tick where the latch was set True before the gate
        # started failing.
        kd_3m = indicators.get("kd_3m")
        if not _kd_3m_ok(kd_3m, ts, p.kd_3m_floor):
            st.last_long_ready = False
            return None
        macd_3m = indicators.get("macd_3m")
        if not _macd_3m_rising(macd_3m, ts):
            st.last_long_ready = False
            return None
        dmi_3m = indicators.get("dmi_3m")
        if not _dmi_3m_ok(dmi_3m, ts):
            st.last_long_ready = False
            return None

        kd = indicators.get("kd")
        macd = indicators.get("macd")
        dmi = indicators.get("dmi")
        if kd is None or macd is None or dmi is None:
            return None

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
        p: TradeStrat1KParams,
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
                "di_jump_points": p.di_jump_points,
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
