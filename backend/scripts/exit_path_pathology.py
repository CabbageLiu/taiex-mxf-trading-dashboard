"""Subagent 2 — Exit-Path Pathology + ONE Frozen Guard.

Reconstructs per-bar PnL paths for every strat_1k trade across the 30-day
baseline window, then sweeps 11 candidate exit guards on TRAIN folds only,
prespecifies ONE winner, and scores it on the corresponding HOLDOUT folds.

Workflow:
  1. Backtest strat_1k across 2026-04-21 → 2026-05-20 (MXF).
  2. For each emitted trade, load 1m bars [entry_ts, exit_ts] (closed bars
     up to and including the exit bar) → per-trade MFE/MAE walk.
  3. Descriptive Wins-vs-Losses tables of {MFE, MAE, time_to_MAE,
     max_bars_in_red}.
  4. Counterfactually replay each trade with one of 11 candidate guards
     layered on top (TP first, then guard, then existing trail). Score per
     train fold. Pick winner.
  5. Score frozen guard on each fold's holdout. Final ship/no-ship verdict.

Output: one big stdout dump + per-trade JSON written to
``backend/scripts/out/exit_path_trades.json`` for downstream inspection.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.api.routes.bars import load_bars
from app.backtest.engine import BacktestTrade, run_backtest
from app.config import get_settings
from app.db.engine import dispose_engine, init_engine
from app.strategies.examples.strat_1k import _exit_params_for
from app.strategies.registry import discover

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.permutation_inference import (
    blocked_permutation_pvalue,
    build_folds,
    parse_utc_date,
)
import numpy as np

UTC = timezone.utc

# --------------------------------------------------------------------------- #
# Configuration (defaults; overridden by CLI in ``main()``)                   #
# --------------------------------------------------------------------------- #

BASELINE_START: datetime = datetime(2025, 11, 19, tzinfo=UTC)
BASELINE_END: datetime = datetime(2026, 5, 21, tzinfo=UTC)  # exclusive
SYMBOL_OVERRIDE: str | None = None

# Filled in by ``main()`` from CLI args.
FOLDS: list[tuple[str, datetime, datetime, datetime, datetime]] = []

# Guard sweep — 11 candidates total.
HARD_SL_PTS = [-12, -16, -20, -25, -30]
TIME_STOP_BARS = [5, 8, 12]
BE_AFTER_PTS = [10, 15, 20]


# --------------------------------------------------------------------------- #
# Per-trade pathology                                                         #
# --------------------------------------------------------------------------- #

@dataclass
class TradePath:
    trade_id: int
    side: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    actual_pnl: float
    actual_exit_reason: str
    bars_held: int
    # path stats
    mfe: float           # peak favorable excursion across the held window
    mae: float           # worst adverse excursion across the held window
    time_to_mfe_bars: int
    time_to_mae_bars: int
    max_bars_in_red_before_green: int
    tp_target: float
    # per-bar arrays so counterfactual sweep doesn't re-fetch
    bars: pd.DataFrame   # 1m OHLC over [entry_ts, exit_ts]


async def _path_for_trade(t: BacktestTrade, symbol: str) -> TradePath | None:
    """Load 1m bars over the trade window and derive MFE/MAE stats."""
    # Pad end by 1 minute so the exit bar is included (load_bars uses
    # exclusive end via DB BETWEEN-style query; safer to over-fetch).
    bars = await load_bars(symbol, "1m",
                           start=t.entry_ts,
                           end=t.exit_ts + timedelta(minutes=1))
    if bars.empty:
        return None
    # Strict held window: bars whose bucket falls in [entry_ts, exit_ts].
    bars = bars[(bars.index >= t.entry_ts) & (bars.index <= t.exit_ts)]
    if bars.empty:
        return None

    sign = 1.0 if t.side == "LONG" else -1.0
    favor = sign * (bars["high"] - t.entry_price) if t.side == "LONG" \
        else sign * (t.entry_price - bars["low"])
    adverse = sign * (bars["low"] - t.entry_price) if t.side == "LONG" \
        else sign * (t.entry_price - bars["high"])

    mfe = float(favor.max())
    mae = float(adverse.min())
    # bar indices (0 = entry bar). argmax/argmin returns relative position.
    try:
        time_to_mfe = int(favor.values.argmax())
    except ValueError:
        time_to_mfe = 0
    try:
        time_to_mae = int(adverse.values.argmin())
    except ValueError:
        time_to_mae = 0

    # max consecutive red-close bars before first green close
    closes = bars["close"].values
    max_red_streak = 0
    cur_red = 0
    seen_green = False
    for px in closes:
        pnl_close = sign * (px - t.entry_price)
        if pnl_close > 0:
            seen_green = True
            break
        cur_red += 1
        max_red_streak = max(max_red_streak, cur_red)
    if not seen_green:
        max_red_streak = len(closes)

    tp_target, _ = _exit_params_for(t.entry_ts, get_settings().tz)

    return TradePath(
        trade_id=t.id,
        side=t.side,
        entry_ts=t.entry_ts,
        exit_ts=t.exit_ts,
        entry_price=t.entry_price,
        exit_price=t.exit_price,
        actual_pnl=t.pnl_points,
        actual_exit_reason=t.exit_reason,
        bars_held=t.bars_held,
        mfe=mfe,
        mae=mae,
        time_to_mfe_bars=time_to_mfe,
        time_to_mae_bars=time_to_mae,
        max_bars_in_red_before_green=max_red_streak,
        tp_target=tp_target,
        bars=bars,
    )


# --------------------------------------------------------------------------- #
# Counterfactual guard replay                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class Guard:
    """One candidate guard layered on top of TP/TRAIL.

    Exactly one of (hard_sl, time_stop_bars, be_after) is non-None.
    Precedence inside a bar: TP first, then guard, then TRAIL.
    """
    label: str
    hard_sl: float | None = None        # absolute pnl floor (negative)
    time_stop_bars: int | None = None   # exit at K-th bar in trade
    be_after: float | None = None       # move stop to entry after +Y gain


def _trail_pts() -> float:
    return 40.0  # matches strat_1k._TRAIL_POINTS


def replay_with_guard(path: TradePath, guard: Guard | None) -> tuple[float, str]:
    """Return (cf_pnl, cf_reason) after applying TP→guard→TRAIL precedence.

    Mirrors strat_1k._manage_open_position intra-bar realism: each bar's
    high/low decide if TP/SL/BE/TRAIL fills *that bar*. TP wins ties, then
    guard, then trail (matching live code where TP check precedes TRAIL
    check, and we inject our guard between them).

    Note: this counterfactual layer overrides the strategy's EXIT signal
    (e.g. reverse->LONG / 'EXIT' from reverse-flip pairs). The pnl path
    walks until either an exit target hits OR we run out of bars (in
    which case we settle at the *original* exit_price — same boundary
    condition the historical reverse-flip created).
    """
    side = path.side
    sign = 1.0 if side == "LONG" else -1.0
    entry = path.entry_price
    tp_pts = path.tp_target
    trail_pts = _trail_pts()

    peak_pnl = 0.0
    be_armed = False

    for i, (_, row) in enumerate(path.bars.iterrows()):
        bar_high = float(row["high"])
        bar_low = float(row["low"])

        # PnL excursions inside the bar.
        if side == "LONG":
            bar_max_pnl = bar_high - entry
            bar_min_pnl = bar_low - entry
        else:
            bar_max_pnl = entry - bar_low
            bar_min_pnl = entry - bar_high

        # 1. TP first (mirrors strat_1k order — TP before trail).
        if bar_max_pnl >= tp_pts:
            return tp_pts, "TP"

        # 2. Guard layer.
        if guard is not None:
            if guard.hard_sl is not None and bar_min_pnl <= guard.hard_sl:
                return float(guard.hard_sl), f"GUARD_SL({guard.hard_sl})"

            if guard.time_stop_bars is not None and i >= guard.time_stop_bars:
                # Exit at this bar's close.
                close_px = float(row["close"])
                close_pnl = sign * (close_px - entry)
                return close_pnl, f"GUARD_TIME({guard.time_stop_bars})"

            if guard.be_after is not None:
                if not be_armed and bar_max_pnl >= guard.be_after:
                    be_armed = True
                if be_armed and bar_min_pnl <= 0.0:
                    return 0.0, f"GUARD_BE({guard.be_after})"

        # 3. TRAIL (post-guard so a tighter guard fires first).
        # peak after this bar's favorable swing
        effective_peak = max(peak_pnl, bar_max_pnl)
        trail_target_pnl = effective_peak - trail_pts
        if bar_min_pnl <= trail_target_pnl:
            return trail_target_pnl, "TRAIL"
        peak_pnl = effective_peak

    # Ran out of bars — settle at the original exit price (engine's exit).
    return path.actual_pnl, "ORIG_EXIT"


# --------------------------------------------------------------------------- #
# Stats                                                                       #
# --------------------------------------------------------------------------- #

def _stats(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {"n": 0, "wr": None, "pnl": 0.0, "pf": None, "max_dd": None}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else None)
    wr = 100.0 * len(wins) / len(pnls)
    pnl = sum(pnls)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {"n": len(pnls), "wr": wr, "pnl": pnl, "pf": pf, "max_dd": max_dd}


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

async def _amain() -> None:
    discover()
    symbol = SYMBOL_OVERRIDE or get_settings().symbol_display

    print(f"=== strat_1k exit-path pathology — {BASELINE_START.date()} → {BASELINE_END.date()} ===\n")

    # 1. Single full-range backtest → enumerate trades.
    bt = await run_backtest(
        strategy_name="strat_1k",
        symbol=symbol,
        start=BASELINE_START,
        end=BASELINE_END,
        params_override=None,
    )
    print(f"baseline backtest: trades={len(bt.trades)} wr={bt.stats.get('win_rate')} "
          f"pnl={bt.stats.get('total_pnl_points')} pf={bt.stats.get('profit_factor')}")

    # 2. Per-trade paths.
    paths: list[TradePath] = []
    for t in bt.trades:
        p = await _path_for_trade(t, symbol)
        if p is not None:
            paths.append(p)
    print(f"reconstructed paths: {len(paths)}/{len(bt.trades)}\n")

    # Dump JSON snapshot (without bars dataframe).
    out_dir = Path(__file__).resolve().parent / "out"
    out_dir.mkdir(exist_ok=True)
    rows_json = [
        {
            "trade_id": p.trade_id,
            "side": p.side,
            "entry_ts": p.entry_ts.isoformat(),
            "exit_ts": p.exit_ts.isoformat(),
            "actual_pnl": p.actual_pnl,
            "exit_reason": p.actual_exit_reason,
            "bars_held": p.bars_held,
            "mfe": p.mfe,
            "mae": p.mae,
            "time_to_mfe_bars": p.time_to_mfe_bars,
            "time_to_mae_bars": p.time_to_mae_bars,
            "max_bars_in_red_before_green": p.max_bars_in_red_before_green,
            "tp_target": p.tp_target,
        }
        for p in paths
    ]
    (out_dir / "exit_path_trades.json").write_text(json.dumps(rows_json, indent=2))
    # Also dump per-brief location for cross-study consumption.
    Path("/tmp/strat_1k_182d_exit_paths.json").write_text(json.dumps(rows_json, indent=2))

    # 3. Descriptive W vs L distributions.
    winners = [p for p in paths if p.actual_pnl > 0]
    losers = [p for p in paths if p.actual_pnl < 0]
    print(f"=== Section 2: W vs L distributions  (n_W={len(winners)} n_L={len(losers)}) ===")
    print(f"{'metric':<28}{'group':>8}{'median':>10}{'IQR':>14}{'P10':>10}{'P90':>10}")
    for label, getter in [
        ("MFE (pts)", lambda x: x.mfe),
        ("MAE (pts)", lambda x: x.mae),
        ("time_to_MAE (bars)", lambda x: x.time_to_mae_bars),
        ("max_bars_in_red", lambda x: x.max_bars_in_red_before_green),
        ("bars_held", lambda x: x.bars_held),
    ]:
        for grp, lst in [("WIN", winners), ("LOSS", losers)]:
            xs = [getter(p) for p in lst]
            if not xs:
                continue
            med = statistics.median(xs)
            q1 = _pct(xs, 25)
            q3 = _pct(xs, 75)
            iqr = f"[{q1:.1f},{q3:.1f}]"
            p10 = _pct(xs, 10)
            p90 = _pct(xs, 90)
            print(f"{label:<28}{grp:>8}{med:>10.2f}{iqr:>14}{p10:>10.2f}{p90:>10.2f}")
    print()

    # 4. Train-fold sweep.
    candidates: list[Guard] = []
    for sl in HARD_SL_PTS:
        candidates.append(Guard(label=f"hard_sl_{sl}", hard_sl=sl))
    for k in TIME_STOP_BARS:
        candidates.append(Guard(label=f"time_stop_{k}", time_stop_bars=k))
    for y in BE_AFTER_PTS:
        candidates.append(Guard(label=f"be_after_{y}", be_after=y))
    assert len(candidates) == 11

    # Baseline counterfactual (no guard) — measures replay-vs-engine drift.
    # NB: replay_with_guard with guard=None preserves TP→TRAIL, but the
    # engine's exits also include reverse-flip & on_tick paths we don't
    # see in the 1m closed bars. Treat baseline_repl as the apples-to-apples
    # comparison row for the guard sweep, NOT the engine's stats.
    train_pnls_baseline_by_fold: dict[str, list[float]] = {}
    holdout_pnls_baseline_by_fold: dict[str, list[float]] = {}

    # Bucket paths by fold (train / test).
    def _in(ts: datetime, lo: datetime, hi: datetime) -> bool:
        return lo <= ts < hi

    train_paths_by_fold: dict[str, list[TradePath]] = {}
    test_paths_by_fold: dict[str, list[TradePath]] = {}
    for name, train_lo, train_hi, test_lo, test_hi in FOLDS:
        train_paths_by_fold[name] = [p for p in paths if _in(p.entry_ts, train_lo, train_hi)]
        test_paths_by_fold[name] = [p for p in paths if _in(p.entry_ts, test_lo, test_hi)]

    # Compute baseline (no-guard) replay stats per fold.
    for name, _, _, _, _ in FOLDS:
        train_pnls_baseline_by_fold[name] = [
            replay_with_guard(p, None)[0] for p in train_paths_by_fold[name]
        ]
        holdout_pnls_baseline_by_fold[name] = [
            replay_with_guard(p, None)[0] for p in test_paths_by_fold[name]
        ]

    # Per-candidate per-fold metrics.
    print("=== Section 3: Train-fold sweep (median across 5 folds, IQR in brackets) ===")
    print(f"{'guard':<18}{'retained%':>11}{'WR':>8}{'PnL':>10}{'PF':>8}{'MaxDD':>9}")

    sweep_summary: dict[str, dict[str, list[float]]] = {}

    # Reference: no-guard baseline median
    base_pfs = []
    base_pnls = []
    base_wrs = []
    base_dds = []
    base_ns = []
    for name, _, _, _, _ in FOLDS:
        s = _stats(train_pnls_baseline_by_fold[name])
        if s["pf"] is not None and s["pf"] != float("inf"):
            base_pfs.append(s["pf"])
        if s["wr"] is not None:
            base_wrs.append(s["wr"])
        base_pnls.append(s["pnl"])
        base_dds.append(s["max_dd"] or 0)
        base_ns.append(s["n"])
    print(f"{'(no-guard)':<18}{100.0:>10.1f}%{statistics.median(base_wrs):>8.2f}"
          f"{statistics.median(base_pnls):>10.1f}"
          f"{statistics.median(base_pfs):>8.3f}{statistics.median(base_dds):>9.1f}")

    for g in candidates:
        wrs, pnls, pfs, dds, retained = [], [], [], [], []
        for name, _, _, _, _ in FOLDS:
            train = train_paths_by_fold[name]
            if not train:
                continue
            cf = [replay_with_guard(p, g)[0] for p in train]
            base_n = len(train)
            s = _stats(cf)
            # "Retained" = trades not auto-flat-zero. Every replay produces
            # a pnl number; for retained_pct we count trades not stopped
            # so early they'd never have been opened. With our guards
            # (all post-entry), n stays equal — but PF-based "non-degenerate"
            # is the same. We track effective trade count = base_n.
            retained.append(100.0 * s["n"] / base_n if base_n > 0 else 0.0)
            if s["wr"] is not None:
                wrs.append(s["wr"])
            pnls.append(s["pnl"])
            if s["pf"] is not None and s["pf"] != float("inf"):
                pfs.append(s["pf"])
            dds.append(s["max_dd"] or 0)
        if not pfs:
            continue
        med_pf = statistics.median(pfs)
        med_wr = statistics.median(wrs) if wrs else 0.0
        med_pnl = statistics.median(pnls)
        med_dd = statistics.median(dds)
        med_ret = statistics.median(retained)
        sweep_summary[g.label] = {
            "pfs": pfs, "wrs": wrs, "pnls": pnls, "dds": dds, "retained": retained,
            "med_pf": med_pf, "med_wr": med_wr, "med_pnl": med_pnl, "med_dd": med_dd,
            "med_ret": med_ret,
        }
        print(f"{g.label:<18}{med_ret:>10.1f}%{med_wr:>8.2f}{med_pnl:>10.1f}"
              f"{med_pf:>8.3f}{med_dd:>9.1f}")
    print()

    # 5. Pre-specify ONE winner.
    # Selection criterion (codex-tightened for n=400/fold):
    #   max train-fold median PF subject to retained ≥ 70%.
    # All guards here are post-entry, so retained == 100% in nominal count;
    # the constraint is satisfied trivially. Tie-break: lower median MaxDD.
    eligible = {
        lbl: s for lbl, s in sweep_summary.items() if s["med_ret"] >= 70.0
    }
    if not eligible:
        print("NO GUARD SATISFIES RETENTION FLOOR — abort selection.")
        return
    # max PF, tie-break min MaxDD
    winner_label, winner_stats = max(
        eligible.items(),
        key=lambda kv: (kv[1]["med_pf"], -kv[1]["med_dd"]),
    )
    winner_guard = next(g for g in candidates if g.label == winner_label)
    print(f"=== Section 4: FROZEN GUARD = {winner_label} ===")
    print(f"  rationale: highest train-fold median PF (={winner_stats['med_pf']:.3f}) "
          f"with retained={winner_stats['med_ret']:.1f}% and MaxDD={winner_stats['med_dd']:.1f}.")
    print()

    # 6. Holdout scoring with frozen guard.
    print("=== Section 5: Holdout scores for FROZEN GUARD ===")
    print(f"{'fold':<8}{'n':>6}{'base_PF':>10}{'guard_PF':>10}{'PF_lift':>10}"
          f"{'base_WR':>10}{'guard_WR':>10}{'WR_lift':>10}{'retained%':>11}")
    holdout_rows: list[dict[str, Any]] = []
    holdout_lifts: list[float] = []
    holdout_wr_lifts: list[float] = []
    holdout_retained: list[float] = []
    folds_better = 0
    for name, _, _, _, _ in FOLDS:
        test = test_paths_by_fold[name]
        if not test:
            print(f"{name:<8} (no trades)")
            continue
        base = _stats(holdout_pnls_baseline_by_fold[name])
        cf = [replay_with_guard(p, winner_guard)[0] for p in test]
        s = _stats(cf)
        base_pf = base["pf"] if base["pf"] not in (None, float("inf")) else 0.0
        cf_pf = s["pf"] if s["pf"] not in (None, float("inf")) else 0.0
        pf_lift = cf_pf - base_pf
        base_wr = base["wr"] or 0.0
        cf_wr = s["wr"] or 0.0
        wr_lift = cf_wr - base_wr
        ret = 100.0 * s["n"] / max(base["n"], 1)
        holdout_lifts.append(pf_lift)
        holdout_wr_lifts.append(wr_lift)
        holdout_retained.append(ret)
        if pf_lift > 0:
            folds_better += 1
        holdout_rows.append({
            "fold": name,
            "n": s["n"],
            "base_pf": base_pf,
            "guard_pf": cf_pf,
            "pf_lift": pf_lift,
            "base_wr": base_wr,
            "guard_wr": cf_wr,
            "wr_lift": wr_lift,
            "retained_pct": ret,
            "base_pnl": base["pnl"],
            "guard_pnl": s["pnl"],
            "base_max_dd": base["max_dd"] or 0,
            "guard_max_dd": s["max_dd"] or 0,
        })
        print(f"{name:<8}{s['n']:>6}{base_pf:>10.3f}{cf_pf:>10.3f}{pf_lift:>+10.3f}"
              f"{base_wr:>10.2f}{cf_wr:>10.2f}{wr_lift:>+10.2f}{ret:>10.1f}%")
    print()

    # 6b. Bootstrap 90% CI on PF lift across folds (1000 iter, seed=42).
    rng = np.random.default_rng(42)
    n_folds_obs = len(holdout_lifts)
    boot_medians: list[float] = []
    if n_folds_obs >= 2:
        lifts_arr = np.asarray(holdout_lifts, dtype=float)
        for _ in range(1000):
            sample = rng.choice(lifts_arr, size=n_folds_obs, replace=True)
            boot_medians.append(float(np.median(sample)))
        ci_lo = float(np.percentile(boot_medians, 5.0))
        ci_hi = float(np.percentile(boot_medians, 95.0))
    else:
        ci_lo = ci_hi = float("nan")

    # 6c. Blocked permutation p-value across folds.
    perm = blocked_permutation_pvalue(holdout_lifts, n_iter=5000, seed=42)

    print("=== Section 6: Bootstrap CI + permutation p ===")
    print(f"  bootstrap 90% CI on PF lift (1000 iter, seed=42): "
          f"[{ci_lo:+.3f}, {ci_hi:+.3f}]")
    print(f"  blocked permutation p (5000 iter, seed=42): "
          f"p_two_sided={perm['p_two_sided']:.4f} "
          f"p_one_sided_greater={perm['p_one_sided_greater']:.4f}")
    print()

    # 7. Verdict (codex-tightened acceptance criteria for n=400/fold).
    print("=== Section 7: VERDICT ===")
    if not holdout_lifts:
        print("  noise, do not ship — no holdout trades.")
        verdict = "noise, do not ship"
        med_lift = float("nan")
        worst_lift = float("nan")
        min_retained = float("nan")
    else:
        med_lift = statistics.median(holdout_lifts)
        worst_lift = min(holdout_lifts)
        min_retained = min(holdout_retained)
        print(f"  median holdout PF lift = {med_lift:+.3f}  (threshold ≥ +0.10)")
        print(f"  worst-fold PF lift     = {worst_lift:+.3f}  (threshold ≥ -0.05)")
        print(f"  min retained%          = {min_retained:.1f}% (threshold ≥ 70%)")
        print(f"  folds with PF lift > 0 = {folds_better}/{len(holdout_lifts)} (need ≥6)")
        ci_excludes_zero = (ci_lo > 0.0) or (ci_hi < 0.0)
        print(f"  90% CI excludes zero   = {ci_excludes_zero}")
        print(f"  blocked permutation p  = {perm['p_one_sided_greater']:.4f} (need ≤ 0.05)")

        ship = (med_lift >= 0.10
                and worst_lift >= -0.05
                and min_retained >= 70.0
                and folds_better >= 6
                and perm['p_one_sided_greater'] <= 0.05
                and ci_excludes_zero)
        promising = (folds_better >= 4 and med_lift > 0 and not ship)

        if ship:
            verdict = f"ship: {winner_label} ({winner_guard.__dict__})"
            print(f"  VERDICT: {verdict}")
        elif promising:
            verdict = "promising but inconclusive"
            reasons = []
            if med_lift < 0.10:
                reasons.append(f"median lift {med_lift:+.3f} < +0.10")
            if worst_lift < -0.05:
                reasons.append(f"worst {worst_lift:+.3f} < -0.05")
            if min_retained < 70.0:
                reasons.append(f"retention {min_retained:.1f}% < 70%")
            if folds_better < 6:
                reasons.append(f"only {folds_better}/{len(holdout_lifts)} folds positive")
            if perm['p_one_sided_greater'] > 0.05:
                reasons.append(f"perm p={perm['p_one_sided_greater']:.4f} > 0.05")
            if not ci_excludes_zero:
                reasons.append("90% CI includes 0")
            print(f"  VERDICT: {verdict} — {', '.join(reasons)}")
        else:
            verdict = "noise, do not ship"
            reasons = []
            if med_lift < 0.10:
                reasons.append(f"median lift {med_lift:+.3f} < +0.10")
            if worst_lift < -0.05:
                reasons.append(f"worst {worst_lift:+.3f} < -0.05")
            if min_retained < 70.0:
                reasons.append(f"retention {min_retained:.1f}% < 70%")
            if folds_better < 6:
                reasons.append(f"only {folds_better}/{len(holdout_lifts)} folds positive")
            if perm['p_one_sided_greater'] > 0.05:
                reasons.append(f"perm p={perm['p_one_sided_greater']:.4f} > 0.05")
            if not ci_excludes_zero:
                reasons.append("90% CI includes 0")
            print(f"  VERDICT: {verdict} — {', '.join(reasons)}")

    # 8. Save raw results JSON for downstream codex review.
    raw: dict[str, Any] = {
        "window": {"start": BASELINE_START.isoformat(), "end": BASELINE_END.isoformat()},
        "symbol": symbol,
        "n_folds": len(FOLDS),
        "test_days": (FOLDS[0][4] - FOLDS[0][3]).days if FOLDS else None,
        "baseline_engine": {
            "trades": len(bt.trades),
            "win_rate": bt.stats.get("win_rate"),
            "total_pnl_points": bt.stats.get("total_pnl_points"),
            "profit_factor": bt.stats.get("profit_factor"),
            "max_drawdown": bt.stats.get("max_drawdown"),
        },
        "paths_reconstructed": len(paths),
        "wins_vs_losses": {
            "n_winners": len(winners),
            "n_losers": len(losers),
            "metrics": {
                metric_name: {
                    grp: {
                        "median": statistics.median([getter(p) for p in lst]) if lst else None,
                        "p10": _pct([getter(p) for p in lst], 10) if lst else None,
                        "p25": _pct([getter(p) for p in lst], 25) if lst else None,
                        "p75": _pct([getter(p) for p in lst], 75) if lst else None,
                        "p90": _pct([getter(p) for p in lst], 90) if lst else None,
                    }
                    for grp, lst in (("WIN", winners), ("LOSS", losers))
                }
                for metric_name, getter in [
                    ("mfe", lambda x: x.mfe),
                    ("mae", lambda x: x.mae),
                    ("time_to_mae_bars", lambda x: x.time_to_mae_bars),
                    ("max_bars_in_red", lambda x: x.max_bars_in_red_before_green),
                    ("bars_held", lambda x: x.bars_held),
                ]
            },
        },
        "train_sweep": {
            lbl: {k: v for k, v in s.items() if k not in ("pfs", "wrs", "pnls", "dds", "retained")}
            | {"per_fold_pf": s["pfs"], "per_fold_wr": s["wrs"],
               "per_fold_pnl": s["pnls"], "per_fold_dd": s["dds"],
               "per_fold_retained": s["retained"]}
            for lbl, s in sweep_summary.items()
        },
        "frozen_guard": {
            "label": winner_label,
            "hard_sl": winner_guard.hard_sl,
            "time_stop_bars": winner_guard.time_stop_bars,
            "be_after": winner_guard.be_after,
            "train_med_pf": winner_stats["med_pf"],
            "train_med_dd": winner_stats["med_dd"],
            "train_med_ret": winner_stats["med_ret"],
        },
        "holdout": holdout_rows,
        "bootstrap": {
            "n_iter": 1000,
            "seed": 42,
            "ci90_lo": ci_lo,
            "ci90_hi": ci_hi,
        },
        "permutation": dict(perm),
        "summary": {
            "median_pf_lift": med_lift if holdout_lifts else None,
            "worst_pf_lift": worst_lift if holdout_lifts else None,
            "min_retained": min_retained if holdout_lifts else None,
            "folds_better": folds_better,
            "n_folds_holdout": len(holdout_lifts),
            "verdict": verdict,
        },
    }
    out_path = Path("/tmp/strat_1k_182d_s2_exit_guard.json")
    out_path.write_text(json.dumps(raw, indent=2, default=str))
    print(f"\nSaved raw results to {out_path}")


async def main() -> None:
    await init_engine()
    try:
        await _amain()
    finally:
        await dispose_engine()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="strat_1k exit-path pathology + frozen-guard walk-forward",
    )
    ap.add_argument("--start", default="2025-11-19",
                    help="ISO date (UTC) — window start. Default 2025-11-19.")
    ap.add_argument("--end", default="2026-05-21",
                    help="ISO date (UTC) — window end (exclusive). Default 2026-05-21.")
    ap.add_argument("--n-folds", type=int, default=8,
                    help="Walk-forward fold count. Default 8.")
    ap.add_argument("--test-days", type=int, default=18,
                    help="Days per holdout test slice. Default 18.")
    ap.add_argument("--symbol", default=None,
                    help="DB symbol label. Default falls back to settings.symbol_display (MXF).")
    return ap.parse_args(argv)


def _apply_cli(args: argparse.Namespace) -> None:
    """Mutate module-level config from parsed CLI args."""
    global BASELINE_START, BASELINE_END, SYMBOL_OVERRIDE, FOLDS
    BASELINE_START = parse_utc_date(args.start)
    BASELINE_END = parse_utc_date(args.end)
    SYMBOL_OVERRIDE = args.symbol
    FOLDS = build_folds(BASELINE_START, BASELINE_END, args.n_folds, args.test_days)


if __name__ == "__main__":
    cli_args = _parse_args()
    _apply_cli(cli_args)
    print(
        f"Config: symbol={cli_args.symbol or '(settings default)'} "
        f"window={cli_args.start}→{cli_args.end} "
        f"n_folds={cli_args.n_folds} test_days={cli_args.test_days}"
    )
    asyncio.run(main())
