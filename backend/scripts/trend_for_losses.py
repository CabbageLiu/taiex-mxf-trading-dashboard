"""Classify trend label at entry_ts for every losing trade.

Uses TrendService.classify() against 15m bars loaded up to entry_ts.
Read-only; no DB writes.
"""
from __future__ import annotations

import asyncio
from collections import Counter

from sqlalchemy import text

from app.api.routes.bars import load_bars
from app.db.engine import dispose_engine, init_engine, session_scope
from app.indicators.service import cache as indicator_cache
from app.services.trend import classify


async def main() -> None:
    await init_engine()
    try:
        await _main_inner()
    finally:
        await dispose_engine()


async def _main_inner() -> None:
    async with session_scope() as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT id, strategy, symbol, side, entry_ts, exit_ts,
                           entry_price, exit_price, pnl_points
                    FROM trades
                    WHERE exit_ts IS NOT NULL AND pnl_points < 0
                    ORDER BY entry_ts
                    """
                )
            )
        ).mappings().all()

    counts: Counter[str] = Counter()
    per_trade: list[tuple[int, str, str, float]] = []

    for r in rows:
        symbol = r["symbol"]
        entry_ts = r["entry_ts"]
        bars = await load_bars(symbol, "15m", end=entry_ts, limit=200)
        if bars.empty or len(bars) < 50:
            label = "INSUFFICIENT_BARS"
            counts[label] += 1
            per_trade.append((r["id"], r["strategy"], label, r["pnl_points"]))
            continue
        ema20_df = indicator_cache.get(symbol, "15m", "ma", {"period": 20, "kind": "ema"}, bars)
        ema50_df = indicator_cache.get(symbol, "15m", "ma", {"period": 50, "kind": "ema"}, bars)
        dmi_df = indicator_cache.get(symbol, "15m", "dmi", {"period": 14}, bars)

        def _last(df, col):
            if df.empty or col not in df:
                return None
            s = df[col].dropna()
            return None if s.empty else float(s.iloc[-1])

        ema20 = _last(ema20_df, "ma")
        ema50 = _last(ema50_df, "ma")
        plus_di = _last(dmi_df, "plus_di")
        minus_di = _last(dmi_df, "minus_di")
        adx = _last(dmi_df, "adx")
        if None in (ema20, ema50, plus_di, minus_di, adx):
            label = "NAN_INDICATOR"
            counts[label] += 1
            per_trade.append((r["id"], r["strategy"], label, r["pnl_points"]))
            continue
        _, _, label = classify(ema20, ema50, plus_di, minus_di, adx)
        counts[label] += 1
        per_trade.append((r["id"], r["strategy"], label, r["pnl_points"]))

    total = len(rows)
    flat = counts.get("盤整", 0)
    down_mild = counts.get("溫和下降", 0)
    down_strong = counts.get("強勢下降", 0)
    flat_or_down = flat + down_mild + down_strong

    print(f"total losing trades: {total}")
    print("--- distribution ---")
    for lbl in ("強勢上升", "溫和上升", "盤整", "溫和下降", "強勢下降", "INSUFFICIENT_BARS", "NAN_INDICATOR"):
        c = counts.get(lbl, 0)
        pct = (100.0 * c / total) if total else 0.0
        print(f"  {lbl:<18} {c:>3}  ({pct:5.1f}%)")
    print("--- target buckets ---")
    print(f"  平緩 (盤整):              {flat}")
    print(f"  下降 (溫和下降+強勢下降):  {down_mild + down_strong}")
    print(f"  平緩 + 下降:             {flat_or_down}  ({100.0 * flat_or_down / total:.1f}% of {total})")

    # per-strategy breakdown
    by_strat: dict[str, Counter[str]] = {}
    for _id, strat, label, _pnl in per_trade:
        by_strat.setdefault(strat, Counter())[label] += 1
    print("--- per strategy ---")
    for strat, c in sorted(by_strat.items()):
        st_total = sum(c.values())
        st_target = c.get("盤整", 0) + c.get("溫和下降", 0) + c.get("強勢下降", 0)
        print(f"  {strat:<20} losses={st_total:>3}  平緩+下降={st_target}  ({100.0 * st_target / st_total:.1f}%)")


if __name__ == "__main__":
    asyncio.run(main())
