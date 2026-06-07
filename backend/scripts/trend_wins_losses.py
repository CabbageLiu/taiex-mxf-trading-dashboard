"""Group all closed trades by 15m-trend-at-entry and outcome (win/loss).

Emits ASCII table + PNG bar chart to /app/scripts/out/trend_wins_losses.png.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

from app.api.routes.bars import load_bars
from app.db.engine import dispose_engine, init_engine, session_scope
from app.indicators.service import cache as ic
from app.services.trend import classify

LABELS = ["強勢上升", "溫和上升", "盤整", "溫和下降", "強勢下降"]


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
                    SELECT id, strategy, symbol, side, entry_ts, pnl_points
                    FROM trades
                    WHERE exit_ts IS NOT NULL AND pnl_points IS NOT NULL
                      AND entry_ts >= '2026-05-08'::timestamptz
                    ORDER BY entry_ts
                    """
                )
            )
        ).mappings().all()

    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"win": 0, "loss": 0})
    skipped = 0

    for r in rows:
        bars = await load_bars(r["symbol"], "15m", end=r["entry_ts"], limit=200)
        if bars.empty or len(bars) < 50:
            skipped += 1
            continue
        e20s = ic.get(r["symbol"], "15m", "ma", {"period": 20, "kind": "ema"}, bars)["ma"].dropna()
        e50s = ic.get(r["symbol"], "15m", "ma", {"period": 50, "kind": "ema"}, bars)["ma"].dropna()
        dmi = ic.get(r["symbol"], "15m", "dmi", {"period": 14}, bars)
        pdi = dmi["plus_di"].dropna()
        mdi = dmi["minus_di"].dropna()
        adx = dmi["adx"].dropna()
        if e20s.empty or e50s.empty or pdi.empty or mdi.empty or adx.empty:
            skipped += 1
            continue
        _, _, label = classify(
            float(e20s.iloc[-1]),
            float(e50s.iloc[-1]),
            float(pdi.iloc[-1]),
            float(mdi.iloc[-1]),
            float(adx.iloc[-1]),
        )
        outcome = "win" if float(r["pnl_points"]) >= 0 else "loss"
        counts[label][outcome] += 1

    # ---- table
    print(f"{'label':<10}{'wins':>6}{'losses':>8}{'total':>7}{'win%':>7}")
    grand_w = grand_l = 0
    for lbl in LABELS:
        w = counts[lbl]["win"]
        l = counts[lbl]["loss"]
        t = w + l
        wp = (100.0 * w / t) if t else 0.0
        grand_w += w
        grand_l += l
        print(f"{lbl:<10}{w:>6}{l:>8}{t:>7}{wp:>6.1f}%")
    gt = grand_w + grand_l
    print(f"{'TOTAL':<10}{grand_w:>6}{grand_l:>8}{gt:>7}"
          f"{(100.0*grand_w/gt if gt else 0):>6.1f}%")
    print(f"skipped (insufficient bars / NaN): {skipped}")

    # ---- chart
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # CJK fallback — backend container has DejaVu only; render labels as
    # romanised so chart doesn't show □ boxes.
    rom = {
        "強勢上升": "Strong Up",
        "溫和上升": "Mild Up",
        "盤整":     "Sideways",
        "溫和下降": "Mild Down",
        "強勢下降": "Strong Down",
    }
    xs = [rom[l] for l in LABELS]
    wins = [counts[l]["win"] for l in LABELS]
    losses = [counts[l]["loss"] for l in LABELS]

    import numpy as np
    x = np.arange(len(LABELS))
    width = 0.38
    fig, ax = plt.subplots(figsize=(10, 5.5))
    bw = ax.bar(x - width / 2, wins, width, label="Wins", color="#2e7d32")
    bl = ax.bar(x + width / 2, losses, width, label="Losses", color="#c62828")
    ax.set_xticks(x)
    ax.set_xticklabels(xs)
    ax.set_ylabel("trade count")
    ax.set_title("Wins vs Losses by 15m trend at entry  (all closed trades)")
    ax.legend()
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    for bars in (bw, bl):
        for b in bars:
            h = b.get_height()
            if h:
                ax.text(b.get_x() + b.get_width() / 2, h + 0.5, str(int(h)),
                        ha="center", va="bottom", fontsize=9)

    # win-rate overlay
    ax2 = ax.twinx()
    rates = [
        (100.0 * counts[l]["win"] / (counts[l]["win"] + counts[l]["loss"]))
        if (counts[l]["win"] + counts[l]["loss"]) else 0.0
        for l in LABELS
    ]
    ax2.plot(x, rates, marker="o", color="#1565c0", linewidth=1.6, label="Win %")
    ax2.set_ylabel("win %")
    ax2.set_ylim(0, 100)
    for xi, r in zip(x, rates, strict=False):
        ax2.text(xi, r + 2, f"{r:.0f}%", color="#1565c0", ha="center", fontsize=9)

    plt.tight_layout()
    out_dir = Path("/app/scripts/out")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "trend_wins_losses.png"
    plt.savefig(out_path, dpi=140)
    print(f"chart saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
