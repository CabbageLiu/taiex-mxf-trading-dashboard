"""Deep analysis of trade outcomes by 15m trend bucket.

For each closed trade, compute at entry_ts:
  - trend label
  - ADX, +DI, -DI
  - EMA20/EMA50 spread (pct of price)
  - price vs EMA20 (above/below, pct)
  - price vs EMA50 (above/below, pct)
  - is ADX rising (last 3 bars slope > 0)

Then bucket by (trend, win/loss) and report:
  - n, avg pnl, median pnl, avg ADX, avg %above_ema20, avg %above_ema50
  - exit reason distribution (from signals.payload.reason)
"""
from __future__ import annotations

import asyncio
import statistics
from collections import Counter, defaultdict

from sqlalchemy import text

from app.api.routes.bars import load_bars
from app.db.engine import dispose_engine, init_engine, session_scope
from app.indicators.service import cache as ic
from app.services.trend import classify

LABELS = ["強勢上升", "溫和上升", "盤整", "溫和下降", "強勢下降"]


def _last(s, n: int = 1):
    s = s.dropna()
    if s.empty or len(s) < n:
        return None
    return float(s.iloc[-n])


async def main() -> None:
    await init_engine()
    try:
        await _run()
    finally:
        await dispose_engine()


async def _run() -> None:
    async with session_scope() as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT t.id, t.strategy, t.symbol, t.side,
                           t.entry_ts, t.exit_ts, t.entry_price, t.exit_price,
                           t.pnl_points, t.exit_signal_id,
                           sx.payload AS exit_payload
                    FROM trades t
                    LEFT JOIN signals sx ON sx.id = t.exit_signal_id
                    WHERE t.exit_ts IS NOT NULL AND t.pnl_points IS NOT NULL
                      AND t.entry_ts >= '2026-05-08'::timestamptz
                    ORDER BY t.entry_ts
                    """
                )
            )
        ).mappings().all()

    # bucket[label][win|loss] = list of dicts
    bk: dict[str, dict[str, list[dict]]] = {
        l: {"win": [], "loss": []} for l in LABELS
    }
    bars_in_trade: dict[str, dict[str, list[float]]] = {
        l: {"win": [], "loss": []} for l in LABELS
    }
    exit_reasons: dict[str, dict[str, Counter]] = {
        l: {"win": Counter(), "loss": Counter()} for l in LABELS
    }

    for r in rows:
        bars = await load_bars(r["symbol"], "15m", end=r["entry_ts"], limit=200)
        if bars.empty or len(bars) < 50:
            continue
        e20 = ic.get(r["symbol"], "15m", "ma", {"period": 20, "kind": "ema"}, bars)["ma"]
        e50 = ic.get(r["symbol"], "15m", "ma", {"period": 50, "kind": "ema"}, bars)["ma"]
        dmi = ic.get(r["symbol"], "15m", "dmi", {"period": 14}, bars)
        ema20 = _last(e20)
        ema50 = _last(e50)
        plus = _last(dmi["plus_di"])
        minus = _last(dmi["minus_di"])
        adx = _last(dmi["adx"])
        adx_prev3 = _last(dmi["adx"], n=4)
        if None in (ema20, ema50, plus, minus, adx):
            continue
        _, _, label = classify(ema20, ema50, plus, minus, adx)
        entry_px = float(r["entry_price"])
        pct_above_e20 = 100.0 * (entry_px - ema20) / entry_px
        pct_above_e50 = 100.0 * (entry_px - ema50) / entry_px
        ema_spread_pct = 100.0 * (ema20 - ema50) / entry_px
        adx_rising = (adx > adx_prev3) if adx_prev3 is not None else None
        pnl = float(r["pnl_points"])
        outcome = "win" if pnl >= 0 else "loss"
        bk[label][outcome].append(
            {
                "id": r["id"],
                "pnl": pnl,
                "adx": adx,
                "adx_rising": adx_rising,
                "plus_di": plus,
                "minus_di": minus,
                "pct_above_e20": pct_above_e20,
                "pct_above_e50": pct_above_e50,
                "ema_spread_pct": ema_spread_pct,
                "entry_px": entry_px,
                "exit_px": float(r["exit_price"]),
            }
        )
        # bars in trade (15m bars covered)
        dt = (r["exit_ts"] - r["entry_ts"]).total_seconds() / 60.0
        bars_in_trade[label][outcome].append(dt)
        # exit reason
        payload = r["exit_payload"] or {}
        reason = payload.get("reason") if isinstance(payload, dict) else None
        exit_reasons[label][outcome][reason or "?"] += 1

    # ---------------- print
    def avg(xs, key=None):
        if not xs:
            return float("nan")
        vals = [x[key] for x in xs] if key else xs
        return sum(vals) / len(vals)

    def med(xs, key=None):
        if not xs:
            return float("nan")
        vals = [x[key] for x in xs] if key else xs
        return statistics.median(vals)

    print("=== Wins / Losses summary per trend ===")
    hdr = f"{'trend':<8}{'side':<6}{'n':>4}{'avg_pnl':>9}{'med_pnl':>9}{'avg_adx':>9}{'%rise_adx':>11}{'%>e20':>8}{'%>e50':>8}{'spread%':>9}{'avg_min':>9}"
    print(hdr)
    for lbl in LABELS:
        for outcome in ("win", "loss"):
            xs = bk[lbl][outcome]
            if not xs:
                continue
            rising = sum(1 for x in xs if x["adx_rising"]) / len(xs) * 100.0
            print(
                f"{lbl:<8}{outcome:<6}{len(xs):>4}"
                f"{avg(xs,'pnl'):>9.1f}{med(xs,'pnl'):>9.1f}"
                f"{avg(xs,'adx'):>9.1f}{rising:>10.0f}%"
                f"{avg(xs,'pct_above_e20'):>8.2f}"
                f"{avg(xs,'pct_above_e50'):>8.2f}"
                f"{avg(xs,'ema_spread_pct'):>9.2f}"
                f"{avg(bars_in_trade[lbl][outcome]):>9.1f}"
            )

    print()
    print("=== Exit reason distribution per trend & outcome ===")
    for lbl in LABELS:
        for outcome in ("win", "loss"):
            c = exit_reasons[lbl][outcome]
            if not sum(c.values()):
                continue
            total = sum(c.values())
            parts = ", ".join(f"{k}={v}" for k, v in c.most_common())
            print(f"  {lbl:<6} {outcome:<5} ({total:>3}): {parts}")


if __name__ == "__main__":
    asyncio.run(main())
