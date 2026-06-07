#!/usr/bin/env python
"""Replay enabled strategies against historical bars for postmortem analysis.

Usage::

    uv run python scripts/replay_strategies.py --date 2026-05-05
    uv run python scripts/replay_strategies.py --start 2026-05-04T00:00 --end 2026-05-05T16:00
    uv run python scripts/replay_strategies.py --date 2026-05-05 --strategies strat_15k,strat_30k

For each enabled strategy and its primary resolution, the script:
1. Loads closed bars over the requested window via the cagg-backed
   ``load_bars`` (independent of the live runner's in-memory buffer).
2. Iterates each closed-bar boundary chronologically; uses backtest's
   ``_swap_state`` to isolate replay state from any live process.
3. Builds a ``BarEvent`` with primary + aux indicators (same shapes the
   live framework provides).
4. Calls ``cls.on_bar(ev)``; prints any ``Signal`` returned.

This script never persists or dispatches — read-only postmortem aid.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from app.api.routes.bars import load_bars
from app.backtest.engine import _restore_state, _swap_state
from app.config import get_settings
from app.db.engine import dispose_engine, init_engine, session_scope
from app.indicators.service import cache as indicator_cache
from app.ingest.runner import RESOLUTION_DELTAS
from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.registry import all_strategies, discover

log = logging.getLogger("taiex.replay")


@dataclass
class _Hit:
    strategy: str
    resolution: str
    bucket: datetime
    side: str
    price: float
    reason: str


async def _enabled_names() -> set[str]:
    from sqlalchemy import select

    from app.db.models import StrategyConfig

    async with session_scope() as s:
        rows = (await s.execute(select(StrategyConfig))).scalars().all()
    return {r.name for r in rows if r.enabled}


def _primary_resolution(cls: type[Strategy]) -> str | None:
    if cls.tick_resolutions:
        return cls.tick_resolutions[0]
    if cls.resolutions:
        return cls.resolutions[0]
    return None


async def _replay_strategy(
    cls: type[Strategy],
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    bar_window: int,
) -> list[_Hit]:
    primary = _primary_resolution(cls)
    if primary is None:
        return []

    # Load full bar history for primary + each aux resolution. We need
    # bars *before* `start` too so the indicator window is warm at the
    # first replayed bucket — pad by 600 bars worth of duration.
    primary_bars = await load_bars(symbol, primary, end=end, limit=bar_window + 600)
    if primary_bars.empty:
        return []
    aux_bars: dict[str, pd.DataFrame] = {}
    for label, spec in cls.aux_indicator_specs.items():
        res = spec["resolution"]
        if res not in aux_bars:
            aux_bars[res] = await load_bars(
                symbol, res, end=end, limit=bar_window + 600
            )

    primary_delta = RESOLUTION_DELTAS.get(primary)
    hits: list[_Hit] = []
    # Iterate every bar boundary inside [start, end] — replay assumes the
    # framework would have dispatched ``on_bar`` at that bucket close.
    for ts in primary_bars.index:
        bucket = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if bucket < start or bucket > end:
            continue
        # Bars visible at the moment this bucket closed: everything up to
        # AND INCLUDING `bucket` (the just-closed bar).
        sub = primary_bars.loc[:bucket]
        if len(sub) < 2:
            continue
        # Wall-clock time at which the primary bucket finishes — used to
        # filter aux bars to only those that have ALSO finished by that
        # moment. Without this, an aux bucket that started earlier but is
        # still in-progress (e.g. 30m aux against a 15m primary at the
        # primary's mid-point boundary) would leak into the indicator
        # window and diverge from the live framework's view (which only
        # sees closed buckets via ``IngestRunner.snapshot_bars``).
        primary_close = bucket + primary_delta if primary_delta is not None else bucket
        indicators: dict[str, pd.DataFrame] = {}
        for label, spec in cls.indicator_specs.items():
            kind = spec["kind"]
            params = spec.get("params", {})
            indicators[label] = indicator_cache.get(symbol, primary, kind, params, sub)
        for label, spec in cls.aux_indicator_specs.items():
            res = spec["resolution"]
            aux_delta = RESOLUTION_DELTAS.get(res)
            if aux_delta is None:
                indicators[label] = pd.DataFrame()
                continue
            # Only include aux buckets whose CLOSE time is at or before
            # primary close: ``aux_bucket + aux_delta <= primary_close``.
            cutoff = primary_close - aux_delta
            sub_aux = aux_bars[res].loc[:cutoff]
            if sub_aux.empty:
                indicators[label] = pd.DataFrame()
                continue
            kind = spec["kind"]
            params = spec.get("params", {})
            indicators[label] = indicator_cache.get(symbol, res, kind, params, sub_aux)

        try:
            params_obj = cls.params_schema()
        except Exception:
            log.exception("invalid default params for %s", cls.name)
            continue

        ev = BarEvent(
            symbol=symbol,
            resolution=primary,
            bucket=bucket,
            bars=sub,
            indicators=indicators,
        )

        # Synchronous critical section — same isolation pattern the live
        # detector uses. Prevents any leak into a concurrent live process
        # if this script is run against the same Python module.
        saved = _swap_state(cls.name, symbol, cls)
        try:
            strat: Strategy = cls(params=params_obj)
            sig: Signal | None = strat.on_bar(ev)
        except Exception:
            log.exception("strategy %s on_bar raised at %s", cls.name, bucket)
            sig = None
        finally:
            _restore_state(saved)

        if sig is not None:
            hits.append(
                _Hit(
                    strategy=cls.name,
                    resolution=primary,
                    bucket=bucket,
                    side=sig.side,
                    price=sig.price,
                    reason=sig.reason,
                )
            )
    return hits


def _parse_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.date:
        d = datetime.fromisoformat(args.date)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        start = d.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start, end
    if not args.start or not args.end:
        raise SystemExit("either --date or both --start and --end required")
    s = datetime.fromisoformat(args.start)
    e = datetime.fromisoformat(args.end)
    if s.tzinfo is None:
        s = s.replace(tzinfo=UTC)
    if e.tzinfo is None:
        e = e.replace(tzinfo=UTC)
    return s, e


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await init_engine()
    try:
        discover()
        all_known = all_strategies()
        if args.strategies:
            requested = {n.strip() for n in args.strategies.split(",") if n.strip()}
            unknown = requested - all_known.keys()
            if unknown:
                print(f"unknown strategies: {sorted(unknown)}")
                return 2
            chosen = {n: all_known[n] for n in requested}
        else:
            enabled = await _enabled_names()
            chosen = {n: c for n, c in all_known.items() if n in enabled}

        if not chosen:
            print("no strategies selected (none enabled, or all filtered out)")
            return 0

        start, end = _parse_window(args)
        symbol = get_settings().symbol_display

        print(
            f"Replay {symbol} window=[{start.isoformat()}, {end.isoformat()}] "
            f"strategies={sorted(chosen.keys())}"
        )
        all_hits: list[_Hit] = []
        for name, cls in sorted(chosen.items()):
            hits = await _replay_strategy(
                cls,
                symbol=symbol,
                start=start,
                end=end,
                bar_window=args.bar_window,
            )
            print(f"  {name}: {len(hits)} hypothetical signal(s)")
            for h in hits:
                print(
                    f"    {h.bucket.isoformat()}  {h.strategy:<10s} {h.resolution:>4s} "
                    f"{h.side:<5s} @ {h.price:.2f}  reason={h.reason}"
                )
            all_hits.extend(hits)

        print(f"total: {len(all_hits)} hypothetical signal(s)")
        return 0
    finally:
        await dispose_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay strategies for postmortem analysis")
    parser.add_argument("--date", help="UTC date YYYY-MM-DD (full-day window)")
    parser.add_argument("--start", help="UTC ISO start (use with --end)")
    parser.add_argument("--end", help="UTC ISO end (use with --start)")
    parser.add_argument(
        "--strategies",
        help="comma-separated strategy names (default: all enabled)",
    )
    parser.add_argument("--bar-window", type=int, default=500)
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
