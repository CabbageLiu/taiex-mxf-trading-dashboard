"""TradingView-style strategy backtest engine.

Replays a registered strategy across closed historical bars and produces
trades + Pine-Script-Strategy-Tester-style stats. Pure replay — does NOT
write to the live ``trades`` / ``signals`` tables, does NOT mutate live
in-process state (module-level ``_STATE`` dicts on stateful strategies are
saved-and-restored around the run).

v1 deviations from a full Pine-Script strategy:
  * Fills at signal-bar close (no ``next_bar_open``).
  * No commission / slippage / position sizing — 1 contract, points-only PnL.
  * No intraday TP/SL via tick replay — exits are evaluated on the same
    schedule as the strategy ``on_bar`` callbacks (the strategy must encode
    its TP/SL checks in ``on_bar`` for them to fire during a backtest).
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pandas as pd
from pydantic import BaseModel

from app.api.routes.bars import load_bars
from app.api.routes.trades import compute_stats
from app.indicators.service import cache as indicator_cache
from app.ingest.runner import RESOLUTIONS as ALL_RESOLUTIONS
from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.registry import all_strategies

# Tie-break order when multiple resolutions close at the exact same ts.
# Smaller resolutions fire first so finer-grained logic runs ahead of
# coarser-grained logic on a shared bucket boundary.
_RES_RANK = {res: i for i, res in enumerate(ALL_RESOLUTIONS)}


_BACKTEST_CACHE_MAX = 64
_backtest_cache: OrderedDict[tuple, BacktestResult] = OrderedDict()


def _module_mtime(cls: type[Strategy]) -> float:
    """File mtime of the strategy module — fingerprint for cache invalidation
    on hot-reload of edited strategy code."""
    src = inspect.getsourcefile(cls)
    if src is None:
        return 0.0
    try:
        return os.path.getmtime(src)
    except OSError:
        return 0.0


def _params_hash(params_override: dict[str, Any] | None) -> str:
    """Stable hash of the params override dict (or empty)."""
    if not params_override:
        return ""
    return json.dumps(params_override, sort_keys=True, default=str)


def _cache_key(
    *,
    strategy_name: str,
    params_override: dict[str, Any] | None,
    symbol: str | None,
    start: datetime,
    end: datetime,
    mtime: float,
) -> tuple:
    return (
        strategy_name,
        _params_hash(params_override),
        symbol or "",
        start.isoformat(),
        end.isoformat(),
        round(mtime, 3),
    )


def _cache_get(key: tuple) -> BacktestResult | None:
    res = _backtest_cache.get(key)
    if res is not None:
        _backtest_cache.move_to_end(key)
    return res


def _cache_put(key: tuple, value: BacktestResult) -> None:
    _backtest_cache[key] = value
    _backtest_cache.move_to_end(key)
    while len(_backtest_cache) > _BACKTEST_CACHE_MAX:
        _backtest_cache.popitem(last=False)


def clear_backtest_cache() -> None:
    """Test/utility hook — drop all cached results."""
    _backtest_cache.clear()


class BacktestSignalOut(BaseModel):
    ts: datetime
    side: str
    price: float
    resolution: str
    reason: str
    payload: dict[str, Any]


class BacktestTrade(BaseModel):
    id: int
    side: str  # "LONG" | "SHORT"
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    pnl_points: float
    hold_seconds: float
    bars_held: int
    entry_reason: str
    exit_reason: str


class BacktestResult(BaseModel):
    strategy: str
    symbol: str
    start: datetime
    end: datetime
    params: dict[str, Any]
    resolutions: list[str]
    bar_counts: dict[str, int]
    signals: list[BacktestSignalOut]
    trades: list[BacktestTrade]
    stats: dict[str, Any]
    equity_curve: list[dict[str, Any]]


@dataclass
class _OpenPosition:
    side: str
    entry_ts: datetime
    entry_price: float
    entry_reason: str
    entry_resolution: str


def _swap_state(strategy_name: str, symbol: str, cls: type[Strategy]) -> Any:
    """Save and clear a strategy's module-level state for (name, symbol).

    Convention: a strategy module may define ``_STATE: dict`` keyed by
    ``(strategy_name, symbol)``. If present, replace the entry with a fresh
    factory-built one for the duration of the backtest, then restore.
    """
    mod = sys.modules.get(cls.__module__)
    if mod is None:
        return None
    state = getattr(mod, "_STATE", None)
    if state is None or not isinstance(state, dict):
        return None
    key = (strategy_name, symbol)
    saved = state.pop(key, None)
    return (state, key, saved)


def _restore_state(saved: Any) -> None:
    if saved is None:
        return
    state, key, prev = saved
    state.pop(key, None)
    if prev is not None:
        state[key] = prev


def pair_into_trades(
    signals: list[Signal],
    bar_indexes: dict[str, pd.DatetimeIndex],
) -> list[BacktestTrade]:
    """Walk signals chronologically, pair entries with exits.

    Mirrors the live PositionTracker rules: same-direction signals are
    no-ops while a position is open; opposite-direction closes the open
    trade and opens a new one at the same price/ts; EXIT/FLAT closes any
    open position. Same-id replays are not a concern here — the engine
    feeds each signal exactly once.
    """
    trades: list[BacktestTrade] = []
    open_pos: _OpenPosition | None = None
    next_id = 1

    def _bars_held_for(open_pos: _OpenPosition, exit_ts: datetime) -> int:
        idx = bar_indexes.get(open_pos.entry_resolution)
        if idx is None:
            return 0
        # Count bar boundaries strictly inside [entry_ts, exit_ts).
        mask = (idx >= open_pos.entry_ts) & (idx < exit_ts)
        return int(mask.sum())

    def _close(exit_ts: datetime, exit_price: float, exit_reason: str) -> None:
        nonlocal open_pos, next_id
        if open_pos is None:
            return
        sign = 1.0 if open_pos.side == "LONG" else -1.0
        pnl = sign * (exit_price - open_pos.entry_price)
        hold = (exit_ts - open_pos.entry_ts).total_seconds()
        trades.append(
            BacktestTrade(
                id=next_id,
                side=open_pos.side,
                entry_ts=open_pos.entry_ts,
                entry_price=open_pos.entry_price,
                exit_ts=exit_ts,
                exit_price=exit_price,
                pnl_points=pnl,
                hold_seconds=hold,
                bars_held=_bars_held_for(open_pos, exit_ts),
                entry_reason=open_pos.entry_reason,
                exit_reason=exit_reason,
            )
        )
        next_id += 1
        open_pos = None

    for s in signals:
        side = s.side
        if side == "LONG" or side == "SHORT":
            if open_pos is None:
                open_pos = _OpenPosition(
                    side=side,
                    entry_ts=s.ts,
                    entry_price=s.price,
                    entry_reason=s.reason,
                    entry_resolution=s.resolution,
                )
            elif open_pos.side != side:
                _close(s.ts, s.price, f"reverse->{side}")
                open_pos = _OpenPosition(
                    side=side,
                    entry_ts=s.ts,
                    entry_price=s.price,
                    entry_reason=s.reason,
                    entry_resolution=s.resolution,
                )
            # same-direction signal while open → no-op.
        elif side in ("EXIT", "FLAT"):
            if open_pos is not None:
                _close(s.ts, s.price, s.reason or side)
        # any other side label is ignored.
    return trades


def compute_backtest_stats(trades: list[BacktestTrade]) -> dict[str, Any]:
    """Reuse compute_stats for shared fields, then add Pine-Script extras."""
    rows = [
        SimpleNamespace(
            id=t.id,
            entry_ts=t.entry_ts,
            exit_ts=t.exit_ts,
            pnl_points=t.pnl_points,
        )
        for t in trades
    ]
    base = compute_stats(rows)

    pnls = [t.pnl_points for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)  # positive magnitude
    profit_factor: float | None
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = None

    largest_win = max(pnls) if pnls else None
    largest_loss = min(pnls) if pnls else None
    avg_bars_in_trade = (
        sum(t.bars_held for t in trades) / len(trades) if trades else None
    )

    base["profit_factor"] = profit_factor
    base["largest_win"] = largest_win
    base["largest_loss"] = largest_loss
    base["avg_bars_in_trade"] = avg_bars_in_trade
    return base


def build_equity_curve(trades: list[BacktestTrade]) -> list[dict[str, Any]]:
    """Cumulative PnL curve, one point per closed trade exit."""
    closed = sorted(trades, key=lambda t: t.exit_ts)
    out: list[dict[str, Any]] = []
    cum = 0.0
    for t in closed:
        cum += t.pnl_points
        out.append(
            {
                "ts": t.exit_ts.isoformat(),
                "cumulative_pnl": cum,
            }
        )
    return out


def _build_indicators(
    cls: type[Strategy], bars_per_res: dict[str, pd.DataFrame], symbol: str
) -> dict[str, dict[str, pd.DataFrame]]:
    """Per-resolution indicator dataframes for the schedule loop.

    Returns a nested dict keyed first on the resolution that owns the
    indicator (the resolution whose bars feed it). Primary indicators
    are computed at every declared resolution; aux indicators are
    computed at their declared `aux_indicator_specs[*].resolution` and
    keyed there so the schedule loop's per-resolution slice picks them
    up at the correct timestamps.
    """
    out: dict[str, dict[str, pd.DataFrame]] = {res: {} for res in bars_per_res}
    for res, bars in bars_per_res.items():
        if bars.empty:
            continue
        for label, spec in cls.indicator_specs.items():
            kind = spec["kind"]
            params = spec.get("params", {})
            out[res][label] = indicator_cache.get(symbol, res, kind, params, bars)
    for label, spec in cls.aux_indicator_specs.items():
        aux_res = spec["resolution"]
        aux_bars = bars_per_res.get(aux_res)
        if aux_bars is None or aux_bars.empty:
            continue
        kind = spec["kind"]
        params = spec.get("params", {})
        out.setdefault(aux_res, {})[label] = indicator_cache.get(
            symbol, aux_res, kind, params, aux_bars
        )
    return out


def _schedule(
    bars_per_res: dict[str, pd.DataFrame],
    primary_resolutions: list[str] | None = None,
) -> list[tuple[pd.Timestamp, str]]:
    """Build the (ts, resolution) tick list driving the backtest replay.

    `primary_resolutions` (when given) restricts the schedule to those
    resolutions — auxiliary-only resolutions (loaded for indicator
    computation but not declared in `cls.resolutions`) MUST NOT trigger
    on_bar dispatch, otherwise the strategy's primary entry/exit logic
    fires at every aux bucket boundary.
    """
    primary = (
        set(primary_resolutions) if primary_resolutions is not None else None
    )
    items: list[tuple[pd.Timestamp, str]] = []
    for res, bars in bars_per_res.items():
        if primary is not None and res not in primary:
            continue
        for ts in bars.index:
            items.append((ts, res))
    items.sort(key=lambda p: (p[0], _RES_RANK.get(p[1], 99)))
    return items


@dataclass
class _RunInputs:
    cls: type[Strategy]
    params: BaseModel
    symbol: str
    start: datetime
    end: datetime
    bars_per_res: dict[str, pd.DataFrame] = field(default_factory=dict)
    inds_per_res: dict[str, dict[str, pd.DataFrame]] = field(default_factory=dict)


async def _gather_inputs(
    cls: type[Strategy], symbol: str, start: datetime, end: datetime
) -> _RunInputs:
    bars_per_res: dict[str, pd.DataFrame] = {}
    for res in cls.resolutions:
        df = await load_bars(symbol, res, start=start, end=end, limit=None)
        bars_per_res[res] = df
    # Aux indicator resolutions (e.g. 5m MACD on a 30m strategy). Load bars
    # for them too so _build_indicators can compute them and the schedule
    # loop can supply ev.indicators[label] sliced up to the current ts.
    for label, spec in cls.aux_indicator_specs.items():
        aux_res = spec["resolution"]
        if aux_res in bars_per_res:
            continue  # already loaded as a primary resolution
        df = await load_bars(symbol, aux_res, start=start, end=end, limit=None)
        bars_per_res[aux_res] = df
    return _RunInputs(
        cls=cls,
        params=cls.params_schema(),  # placeholder; caller overwrites
        symbol=symbol,
        start=start,
        end=end,
        bars_per_res=bars_per_res,
    )


async def run_backtest(
    *,
    strategy_name: str,
    symbol: str | None,
    start: datetime,
    end: datetime,
    params_override: dict[str, Any] | None = None,
) -> BacktestResult:
    cls = all_strategies().get(strategy_name)
    if cls is None:
        raise KeyError(strategy_name)
    if not cls.resolutions:
        raise ValueError(f"strategy {strategy_name} declares no resolutions")
    if start >= end:
        raise ValueError("start must be before end")

    from app.config import get_settings

    sym = symbol or get_settings().symbol_display

    try:
        params = cls.params_schema(**(params_override or {}))
    except Exception as e:  # pydantic validation
        raise ValueError(f"invalid params: {e}") from None

    mtime = _module_mtime(cls)
    key = _cache_key(
        strategy_name=strategy_name,
        params_override=params_override,
        symbol=sym,
        start=start,
        end=end,
        mtime=mtime,
    )
    cached = _cache_get(key)
    if cached is not None:
        return cached

    bars_per_res: dict[str, pd.DataFrame] = {}
    for res in cls.resolutions:
        df = await load_bars(sym, res, start=start, end=end, limit=None)
        bars_per_res[res] = df
    # Aux resolutions (e.g. 2m on a 1m strategy). Without loading these,
    # `_build_indicators` skips every aux indicator and aux-gated entries
    # never fire under backtest. Aux bars are NOT added to the schedule
    # (see `_schedule(primary_resolutions=...)` below) so they only feed
    # indicator computation, not on_bar dispatch.
    for spec in cls.aux_indicator_specs.values():
        aux_res = spec["resolution"]
        if aux_res in bars_per_res:
            continue
        df = await load_bars(sym, aux_res, start=start, end=end, limit=None)
        bars_per_res[aux_res] = df

    if all(df.empty for df in bars_per_res.values()):
        empty = BacktestResult(
            strategy=strategy_name,
            symbol=sym,
            start=start,
            end=end,
            params=params.model_dump(),
            resolutions=list(cls.resolutions),
            bar_counts={r: 0 for r in cls.resolutions},
            signals=[],
            trades=[],
            stats=compute_backtest_stats([]),
            equity_curve=[],
        )
        _cache_put(key, empty)
        return empty

    inds_per_res = _build_indicators(cls, bars_per_res, sym)
    schedule = _schedule(bars_per_res, primary_resolutions=list(cls.resolutions))
    bar_indexes = {
        r: df.index for r, df in bars_per_res.items() if r in cls.resolutions
    }

    saved = _swap_state(strategy_name, sym, cls)
    signals: list[Signal] = []
    try:
        for ts, res in schedule:
            bars_slice = bars_per_res[res].loc[:ts]
            inds_slice = {
                k: df.loc[:ts] for k, df in inds_per_res.get(res, {}).items()
            }
            # Merge aux indicators sliced from their owning resolution so the
            # strategy sees ev.indicators[label] just like the live path.
            for label, spec in cls.aux_indicator_specs.items():
                aux_res = spec["resolution"]
                aux_inds = inds_per_res.get(aux_res, {})
                if label in aux_inds:
                    inds_slice[label] = aux_inds[label].loc[:ts]
            ev = BarEvent(
                symbol=sym,
                resolution=res,
                bucket=ts.to_pydatetime(),
                bars=bars_slice,
                indicators=inds_slice,
            )
            strat = cls(params=params)
            sig = strat.on_bar(ev)
            if sig is not None:
                signals.append(sig)
    finally:
        _restore_state(saved)

    trades = pair_into_trades(signals, bar_indexes)
    stats = compute_backtest_stats(trades)
    equity = build_equity_curve(trades)

    sig_out = [
        BacktestSignalOut(
            ts=s.ts,
            side=s.side,
            price=s.price,
            resolution=s.resolution,
            reason=s.reason,
            payload=s.payload or {},
        )
        for s in signals
    ]

    result = BacktestResult(
        strategy=strategy_name,
        symbol=sym,
        start=start,
        end=end,
        params=params.model_dump(),
        resolutions=list(cls.resolutions),
        bar_counts={r: len(df) for r, df in bars_per_res.items()},
        signals=sig_out,
        trades=trades,
        stats=stats,
        equity_curve=equity,
    )
    _cache_put(key, result)
    return result
