"""Subagent 3 — Descriptive Loss-Concentration Heatmap (strat_1k).

25-cell grid: (ToD bucket × 15m trend_label).
NORMALIZES pnl_per_trade by each cell's TP target to remove the confounding
introduced by the ToD-segmented TP schedule (30/40/50 pts).

NB: Exploratory / hypothesis-generation only. The 25-cell grid is too small
to support a shippable avoid-cell filter. See plan
``/Users/raccoon/.claude/plans/smooth-sniffing-meadow.md`` for the framing.

Outputs:
  /tmp/strat_1k_trade_dump.json   — per-trade enriched dump
  /tmp/strat_1k_loss_heatmap.png  — 3-panel heatmap (pnl_norm / WR / count)
  stdout: cell-count distribution + 25-cell table sorted by pnl_norm asc

Usage (inside taiex-backend container — uses live TimescaleDB):
  docker exec taiex-backend uv run python -m scripts.loss_heatmap_25cell
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import Counter
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import httpx
import matplotlib.pyplot as plt
import numpy as np

from app.api.routes.bars import load_bars
from app.db.engine import dispose_engine, init_engine
from app.indicators.service import cache as ic
from app.services.trend import classify

TPE = ZoneInfo("Asia/Taipei")
UTC = timezone.utc

# Filled in by ``main()`` from CLI; module-level so ``fetch_backtest_trades``
# and ``enrich_trade`` can read without threading config through every call.
SYMBOL: str = "MXF"
START_STR: str = "2025-11-19"
END_STR: str = "2026-05-21"
OUT_PREFIX: str = "strat_1k"

LABELS = ["強勢上升", "溫和上升", "盤整", "溫和下降", "強勢下降"]
LABELS_EN = ["StrongUp", "MildUp", "Range", "MildDown", "StrongDown"]

# ToD buckets — mirror strat_1k._exit_params_for
BUCKETS = [
    ("bucket_1_0845_1030", time(8, 45), time(10, 31), 50.0),
    ("bucket_2_1031_1344", time(10, 31), time(13, 45), 40.0),
    ("bucket_3_1500_1800", time(15, 0), time(18, 1), 30.0),
    ("bucket_4_1801_2330", time(18, 1), time(23, 31), 50.0),
    ("bucket_5_2331_0459", time(23, 31), time(5, 0), 30.0),  # wraps midnight
]


def tod_bucket_for(ts_utc: datetime) -> tuple[str, float] | None:
    t = ts_utc.astimezone(TPE).time()
    for name, start, end, tp in BUCKETS:
        if start <= end:
            if start <= t < end:
                return name, tp
        else:  # wrap (23:31..05:00)
            if t >= start or t < end:
                return name, tp
    return None  # closed gap


def _last(series, n: int = 1):
    s = series.dropna()
    if s.empty or len(s) < n:
        return None
    return float(s.iloc[-n])


async def fetch_backtest_trades() -> list[dict]:
    async with httpx.AsyncClient(timeout=600.0) as cli:
        r = await cli.post(
            "http://127.0.0.1:8000/backtest/run",
            json={
                "strategy": "strat_1k",
                "symbol": SYMBOL,
                "start": START_STR,
                "end": END_STR,
            },
        )
        r.raise_for_status()
        return r.json()["trades"]


async def enrich_trade(symbol: str, t: dict) -> dict | None:
    entry_ts = datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
    exit_ts = datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00"))
    bars = await load_bars(symbol, "15m", end=entry_ts, limit=200)
    if bars.empty or len(bars) < 50:
        return None
    e20 = ic.get(symbol, "15m", "ma", {"period": 20, "kind": "ema"}, bars).get("ma")
    e50 = ic.get(symbol, "15m", "ma", {"period": 50, "kind": "ema"}, bars).get("ma")
    dmi = ic.get(symbol, "15m", "dmi", {"period": 14}, bars)
    ema20 = _last(e20) if e20 is not None else None
    ema50 = _last(e50) if e50 is not None else None
    plus_di = _last(dmi.get("plus_di"))
    minus_di = _last(dmi.get("minus_di"))
    adx = _last(dmi.get("adx"))
    if None in (ema20, ema50, plus_di, minus_di, adx):
        return None
    _, score, label = classify(ema20, ema50, plus_di, minus_di, adx)
    bk = tod_bucket_for(entry_ts)
    if bk is None:
        return None  # closed gap (should not happen given window)
    bucket_name, tp_target = bk
    pnl = float(t["pnl_points"])
    exit_reason = t.get("exit_reason") or ""
    # Classify exit reason: TP / TRAIL / EOW / OTHER
    if "TP" in exit_reason:
        exit_kind = "TP"
    elif "TRAIL" in exit_reason or "trail" in exit_reason:
        exit_kind = "TRAIL"
    elif "EOW" in exit_reason or "eow" in exit_reason:
        exit_kind = "EOW"
    else:
        exit_kind = "OTHER"
    return {
        "id": t["id"],
        "entry_ts": t["entry_ts"],
        "exit_ts": t["exit_ts"],
        "entry_price": t["entry_price"],
        "exit_price": t["exit_price"],
        "pnl_points": pnl,
        "hold_seconds": float(t.get("hold_seconds") or (exit_ts - entry_ts).total_seconds()),
        "tod_bucket": bucket_name,
        "tp_target": tp_target,
        "trend_label": label,
        "trend_score": score,
        "adx_15m": round(adx, 2),
        "plus_di_15m": round(plus_di, 2),
        "minus_di_15m": round(minus_di, 2),
        "exit_kind": exit_kind,
        "exit_reason_raw": exit_reason,
    }


def aggregate_cells(trades: list[dict]) -> dict:
    cells: dict[tuple[str, str], list[dict]] = {}
    for tr in trades:
        key = (tr["tod_bucket"], tr["trend_label"])
        cells.setdefault(key, []).append(tr)

    out: dict[tuple[str, str], dict] = {}
    for (bk, lbl), xs in cells.items():
        n = len(xs)
        wins = sum(1 for x in xs if x["pnl_points"] > 0)
        losses = sum(1 for x in xs if x["pnl_points"] < 0)
        pnl_sum = sum(x["pnl_points"] for x in xs)
        pnl_per_trade = pnl_sum / n
        tp_target = xs[0]["tp_target"]
        pnl_normalized = pnl_per_trade / tp_target
        wr = wins / n if n >= 10 else None
        median_hold = statistics.median([x["hold_seconds"] for x in xs])
        ex_counter = Counter(x["exit_kind"] for x in xs)
        ex_pct = {k: round(100.0 * v / n, 1) for k, v in ex_counter.items()}
        out[(bk, lbl)] = {
            "tod_bucket": bk,
            "trend_label": lbl,
            "tp_target": tp_target,
            "count": n,
            "wins": wins,
            "losses": losses,
            "wr": wr,
            "pnl_sum": pnl_sum,
            "pnl_per_trade": round(pnl_per_trade, 2),
            "pnl_normalized": round(pnl_normalized, 4),
            "median_holding_seconds": int(median_hold),
            "exit_reason_pct": ex_pct,
        }
    return out


def make_grid(cells: dict, metric: str, default=None) -> np.ndarray:
    bk_names = [b[0] for b in BUCKETS]
    grid = np.full((len(bk_names), len(LABELS)), np.nan, dtype=float)
    for i, bk in enumerate(bk_names):
        for j, lbl in enumerate(LABELS):
            c = cells.get((bk, lbl))
            if c is None:
                continue
            v = c.get(metric)
            if v is None:
                continue
            grid[i, j] = float(v)
    return grid


def plot_heatmaps(cells: dict, out_path: str) -> None:
    bk_short = ["b1\n0845-1030\nTP50", "b2\n1031-1344\nTP40", "b3\n1500-1800\nTP30",
                "b4\n1801-2330\nTP50", "b5\n2331-0459\nTP30"]

    pnl_grid = make_grid(cells, "pnl_normalized")
    wr_grid = make_grid(cells, "wr")
    cnt_grid = make_grid(cells, "count")

    fig, axes = plt.subplots(1, 3, figsize=(20, 6))

    def draw(ax, grid, title, cmap, vmin=None, vmax=None, fmt="{:.2f}", mask_low_count=None):
        masked = grid.copy()
        if mask_low_count is not None:
            masked[mask_low_count < 10] = np.nan
        im = ax.imshow(masked, cmap=cmap, vmin=vmin, vmax=vmax, aspect="auto")
        ax.set_xticks(range(len(LABELS)))
        ax.set_xticklabels(LABELS_EN, rotation=30)
        ax.set_yticks(range(len(bk_short)))
        ax.set_yticklabels(bk_short)
        ax.set_title(title)
        for i in range(grid.shape[0]):
            for j in range(grid.shape[1]):
                v = grid[i, j]
                if np.isnan(v):
                    txt = "—"
                else:
                    txt = fmt.format(v)
                # add count overlay too
                cv = cnt_grid[i, j]
                cnt_lbl = "" if np.isnan(cv) else f"\nn={int(cv)}"
                ax.text(j, i, txt + cnt_lbl, ha="center", va="center",
                        fontsize=8, color="black")
        plt.colorbar(im, ax=ax, fraction=0.045)

    draw(axes[0], pnl_grid, "A: pnl_per_trade / tp_target (normalized)",
         "RdYlGn", vmin=-1.0, vmax=1.0, fmt="{:+.2f}")
    draw(axes[1], wr_grid, "B: win-rate (cells with n<10 grayed)",
         "RdYlGn", vmin=0.2, vmax=0.7, fmt="{:.0%}", mask_low_count=cnt_grid)
    draw(axes[2], cnt_grid, "C: trade count per cell",
         "Blues", vmin=0, vmax=max(60, np.nanmax(cnt_grid) if not np.all(np.isnan(cnt_grid)) else 1),
         fmt="{:.0f}")

    fig.suptitle(
        f"strat_1k loss-concentration heatmap — DESCRIPTIVE ONLY "
        f"({START_STR} → {END_STR})",
        fontsize=13,
    )
    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


async def main() -> None:
    await init_engine()
    try:
        print("Fetching backtest trades...")
        trades = await fetch_backtest_trades()
        print(f"  baseline trade count: {len(trades)}")

        enriched: list[dict] = []
        skipped = 0
        for i, t in enumerate(trades):
            r = await enrich_trade(SYMBOL, t)
            if r is None:
                skipped += 1
                continue
            enriched.append(r)
            if (i + 1) % 100 == 0:
                print(f"  enriched {i + 1}/{len(trades)} (skipped so far: {skipped})")
        print(f"Enriched {len(enriched)}/{len(trades)} (skipped {skipped})")

        dump_path = f"/tmp/{OUT_PREFIX}_trade_dump.json"
        with open(dump_path, "w") as f:
            json.dump(enriched, f, indent=2, default=str)
        print(f"Wrote {dump_path}")

        cells = aggregate_cells(enriched)

        # Cell-count distribution
        counts = [c["count"] for c in cells.values()]
        ge20 = sum(1 for x in counts if x >= 20)
        ge10 = sum(1 for x in counts if x >= 10)
        lt10 = sum(1 for x in counts if x < 10)
        # 25 total possible cells; some may be empty (missing from cells dict)
        present = len(cells)
        empty = 25 - present
        print()
        print("=== Cell-count distribution (25 possible cells) ===")
        print(f"  present   = {present}")
        print(f"  empty     = {empty}")
        print(f"  count>=20 = {ge20}")
        print(f"  count>=10 = {ge10}")
        print(f"  count<10  = {lt10}")

        # Full 25-cell table sorted by pnl_normalized asc
        print()
        print("=== Full cell table sorted by pnl_normalized ascending ===")
        header = (f"{'bucket':<22}{'trend':<10}{'n':>4}{'W':>4}{'L':>4}"
                  f"{'wr':>7}{'pnl_sum':>9}{'pnl/tr':>8}{'tp':>4}"
                  f"{'norm':>7}{'med_hold_s':>11}  exit_pct")
        print(header)
        sorted_cells = sorted(cells.values(), key=lambda c: c["pnl_normalized"])
        for c in sorted_cells:
            wr_str = f"{c['wr']:.1%}" if c["wr"] is not None else "—"
            ex_str = ", ".join(f"{k}={v}%" for k, v in c["exit_reason_pct"].items())
            print(
                f"{c['tod_bucket']:<22}{c['trend_label']:<10}"
                f"{c['count']:>4}{c['wins']:>4}{c['losses']:>4}"
                f"{wr_str:>7}{c['pnl_sum']:>9.0f}{c['pnl_per_trade']:>8.1f}"
                f"{int(c['tp_target']):>4}{c['pnl_normalized']:>+7.3f}"
                f"{c['median_holding_seconds']:>11d}  {ex_str}"
            )

        # Save heatmap PNG
        out_png = f"/tmp/{OUT_PREFIX}_loss_heatmap.png"
        plot_heatmaps(cells, out_png)
        print()
        print(f"Wrote heatmap PNG: {out_png}")

        # JSON dump of cells too
        cells_serializable = [c for c in cells.values()]
        cells_path = f"/tmp/{OUT_PREFIX}_loss_cells.json"
        with open(cells_path, "w") as f:
            json.dump(cells_serializable, f, indent=2, default=str)
        print(f"Wrote {cells_path}")
    finally:
        await dispose_engine()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="strat_1k 25-cell descriptive loss heatmap",
    )
    ap.add_argument("--start", default="2025-11-19",
                    help="ISO date (UTC) — window start. Default 2025-11-19.")
    ap.add_argument("--end", default="2026-05-21",
                    help="ISO date (UTC) — window end (exclusive). Default 2026-05-21.")
    ap.add_argument("--symbol", default="MXF",
                    help="DB symbol label. Default MXF.")
    ap.add_argument("--out-prefix", default="strat_1k",
                    help="Output filename prefix under /tmp/. Default strat_1k.")
    # ``--n-folds`` / ``--test-days`` accepted for orchestrator parity but
    # unused here: the heatmap is a single descriptive aggregate, not a
    # walk-forward study.
    ap.add_argument("--n-folds", type=int, default=None,
                    help="(unused; accepted for orchestrator parity)")
    ap.add_argument("--test-days", type=int, default=None,
                    help="(unused; accepted for orchestrator parity)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    cli_args = _parse_args()
    SYMBOL = cli_args.symbol
    START_STR = cli_args.start
    END_STR = cli_args.end
    OUT_PREFIX = cli_args.out_prefix
    print(f"Config: symbol={SYMBOL} window={START_STR}→{END_STR} out_prefix={OUT_PREFIX}")
    asyncio.run(main())
