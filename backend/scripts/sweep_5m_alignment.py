"""Subagent B/C/D — strat_1k 5m-alignment walk-forward + bootstrap.

Phase B (TRAIN folds): score 4 variants {None, macd_hist, di_positive,
above_ema20} on each fold's TRAIN slice. Aggregate metrics across folds
and apply the pre-registered selection criterion (frozen pre-holdout):
    1. max train-fold median PF
    2. subject to retained_pct >= 80% baseline per fold
    3. tie-break: highest train-fold median WR lift

Phase C (HOLDOUT): if Phase B yields a winner, run it on each fold's TEST
slice and capture {baseline_PF, filter_PF, PF_lift, WR_lift,
retained_pct}.

Phase D (BOOTSTRAP): 1000 resamples (seed=42) on the holdout fold lifts
to deliver 90% CI on PF lift, WR lift, PnL lift.

Calls the running backend's POST /backtest/run REST endpoint. Backend
must be reachable at BACKEND_URL.

CLI:

    uv run python -m scripts.sweep_5m_alignment \\
        --start 2025-11-19 --end 2026-05-21 \\
        --n-folds 8 --test-days 18 --symbol MXF \\
        --filters macd_hist,di_positive,above_ema20

Defaults reproduce the smooth-sniffing-meadow Study 4 / 5 windows. Pass
``--filters`` to include the inverted modes for Study 5.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

# Allow `python scripts/sweep_5m_alignment.py` invocation from repo root
# (without `-m`) to still find sibling `scripts/permutation_inference.py`.
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.permutation_inference import build_folds, parse_utc_date

UTC = timezone.utc
BACKEND_URL = "http://127.0.0.1:8000"

# Filled in by ``main()`` from CLI args. Module-level so phase functions
# can read without threading the value through every call.
SYMBOL: str = "MXF"
FOLDS: list[tuple[str, datetime, datetime, datetime, datetime]] = []
FILTERS: list[str | None] = [None, "macd_hist", "di_positive", "above_ema20"]
MIN_RETAINED: float = 0.80

ALL_KNOWN_FILTERS = {
    "macd_hist", "di_positive", "above_ema20",
    "macd_hist_negative", "di_negative", "below_ema20",
}


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_bt(start: datetime, end: datetime, filt: str | None) -> dict[str, Any]:
    """Call backend /backtest/run and return the metrics subset we care about."""
    params: dict[str, Any] = {}
    if filt is not None:
        params["require_5m_alignment"] = filt
    body = {
        "strategy": "strat_1k",
        "symbol": SYMBOL,
        "start": _iso(start),
        "end": _iso(end),
        "params": params,
    }
    r = requests.post(f"{BACKEND_URL}/backtest/run", json=body, timeout=180)
    r.raise_for_status()
    j = r.json()
    s = j.get("stats", {}) or {}
    return {
        "trade_count": s.get("trade_count") or 0,
        "win_rate": s.get("win_rate"),
        "pnl_total": s.get("pnl_total") or 0.0,
        "profit_factor": s.get("profit_factor"),
        "max_drawdown": s.get("max_drawdown"),
    }


def _median_iqr(values: list[float]) -> tuple[float, float, float]:
    """Return (median, q1, q3) ignoring None."""
    arr = [v for v in values if v is not None]
    if not arr:
        return (float("nan"), float("nan"), float("nan"))
    med = float(statistics.median(arr))
    q1 = float(np.percentile(arr, 25))
    q3 = float(np.percentile(arr, 75))
    return (med, q1, q3)


def _pf_safe(stats: dict[str, Any]) -> float | None:
    pf = stats.get("profit_factor")
    if pf is None:
        return None
    if isinstance(pf, float) and (pf != pf or pf == float("inf")):
        # NaN or inf: treat inf as a large positive (no losses); leave NaN as None
        if pf == float("inf"):
            return 10.0  # cap; we only use it for ranking
        return None
    return float(pf)


def phase_b(out: dict[str, Any]) -> dict[str, Any]:
    """Run all (filter, fold) train pairs and aggregate per filter."""
    print("=" * 70)
    print("Phase B — TRAIN-fold sweep")
    print("=" * 70)
    train_results: dict[str, list[dict[str, Any]]] = {}
    for filt in FILTERS:
        key = "baseline" if filt is None else filt
        train_results[key] = []
        for name, tr_start, tr_end, _ho_start, _ho_end in FOLDS:
            stats = run_bt(tr_start, tr_end, filt)
            stats["fold"] = name
            train_results[key].append(stats)
            print(f"  [{key:>12}] {name} train {tr_start.date()}→{tr_end.date()}: "
                  f"n={stats['trade_count']:>4}  WR={stats['win_rate']}  "
                  f"PnL={stats['pnl_total']}  PF={stats['profit_factor']}  "
                  f"DD={stats['max_drawdown']}")
    out["phase_b_train"] = train_results

    # Aggregate per filter: medians + IQR.
    agg: dict[str, dict[str, Any]] = {}
    baseline_trades_per_fold = [r["trade_count"] for r in train_results["baseline"]]
    for key, rows in train_results.items():
        pfs = [_pf_safe(r) for r in rows]
        wrs = [r["win_rate"] for r in rows]
        pnls = [r["pnl_total"] for r in rows]
        dds = [r["max_drawdown"] for r in rows]
        ns = [r["trade_count"] for r in rows]
        retained = []
        for i, n in enumerate(ns):
            base_n = baseline_trades_per_fold[i]
            retained.append((n / base_n) if base_n else None)
        agg[key] = {
            "trade_counts": ns,
            "retained_per_fold": retained,
            "retained_min": min([r for r in retained if r is not None], default=None),
            "median_pf": _median_iqr(pfs)[0],
            "iqr_pf": (_median_iqr(pfs)[1], _median_iqr(pfs)[2]),
            "median_wr": _median_iqr(wrs)[0],
            "median_pnl": _median_iqr(pnls)[0],
            "median_dd": _median_iqr(dds)[0],
            "median_retained_pct": _median_iqr(retained)[0],
        }
    out["phase_b_aggregate"] = agg

    print("\nPer-filter aggregate across 5 train folds:")
    print(f"{'filter':>14}  {'med_PF':>8}  {'IQR_PF':>16}  {'med_WR':>8}  "
          f"{'med_ret':>8}  {'min_ret':>8}")
    for key, a in agg.items():
        iqr = a["iqr_pf"]
        print(f"{key:>14}  {a['median_pf']:>8.3f}  "
              f"({iqr[0]:.3f},{iqr[1]:.3f})    "
              f"{(a['median_wr'] or 0):>8.3f}  "
              f"{(a['median_retained_pct'] or 0):>8.3f}  "
              f"{(a['retained_min'] or 0):>8.3f}")

    # Apply selection criterion.
    baseline_wr = agg["baseline"]["median_wr"] or 0.0
    candidates = []
    for key, a in agg.items():
        if key == "baseline":
            continue
        # Per-fold retention floor (CLI-tunable, default 0.80).
        if a["retained_min"] is None or a["retained_min"] < MIN_RETAINED:
            print(f"  [DISQUALIFIED] {key}: min_retained={a['retained_min']} < {MIN_RETAINED}")
            continue
        candidates.append((key, a))

    winner: str | None = None
    if candidates:
        # Max median PF; tie-break on WR lift.
        candidates.sort(
            key=lambda kv: (kv[1]["median_pf"], (kv[1]["median_wr"] or 0) - baseline_wr),
            reverse=True,
        )
        winner = candidates[0][0]
    out["phase_b_winner"] = winner
    out["phase_b_baseline_median_wr"] = baseline_wr
    print(f"\nSelection: winner = {winner!r}")
    return out


def phase_c(out: dict[str, Any]) -> dict[str, Any]:
    winner = out.get("phase_b_winner")
    if not winner:
        out["phase_c_holdout"] = None
        print("\nPhase C — skipped (no qualifying filter).")
        return out
    print("\n" + "=" * 70)
    print(f"Phase C — HOLDOUT scoring with frozen filter = {winner!r}")
    print("=" * 70)
    rows = []
    for name, _tr_s, _tr_e, ho_start, ho_end in FOLDS:
        baseline = run_bt(ho_start, ho_end, None)
        treated = run_bt(ho_start, ho_end, winner)
        b_pf = _pf_safe(baseline)
        t_pf = _pf_safe(treated)
        b_wr = baseline["win_rate"] or 0.0
        t_wr = treated["win_rate"] or 0.0
        b_n = baseline["trade_count"] or 0
        t_n = treated["trade_count"] or 0
        pf_lift = (t_pf - b_pf) if (b_pf is not None and t_pf is not None) else None
        wr_lift = t_wr - b_wr
        pnl_lift = (treated["pnl_total"] or 0) - (baseline["pnl_total"] or 0)
        retained = (t_n / b_n) if b_n else None
        rows.append({
            "fold": name,
            "baseline_n": b_n, "filter_n": t_n,
            "baseline_PF": b_pf, "filter_PF": t_pf,
            "PF_lift": pf_lift,
            "baseline_WR": b_wr, "filter_WR": t_wr, "WR_lift": wr_lift,
            "baseline_PnL": baseline["pnl_total"], "filter_PnL": treated["pnl_total"],
            "PnL_lift": pnl_lift,
            "retained_pct": retained,
        })
        print(f"  [{name}] base PF={b_pf} n={b_n} PnL={baseline['pnl_total']} | "
              f"filter PF={t_pf} n={t_n} PnL={treated['pnl_total']} | "
              f"PF_lift={pf_lift} WR_lift={wr_lift:.3f} retained={retained}")
    out["phase_c_holdout"] = rows
    return out


def phase_d(out: dict[str, Any], seed: int = 42, n_resamples: int = 1000) -> dict[str, Any]:
    rows = out.get("phase_c_holdout")
    if not rows:
        out["phase_d_bootstrap"] = None
        print("\nPhase D — skipped (no holdout).")
        return out
    print("\n" + "=" * 70)
    print("Phase D — Bootstrap 90% CI")
    print("=" * 70)
    rng = np.random.default_rng(seed)
    pf_lifts = np.array([r["PF_lift"] for r in rows if r["PF_lift"] is not None], dtype=float)
    wr_lifts = np.array([r["WR_lift"] for r in rows], dtype=float)
    pnl_lifts = np.array([r["PnL_lift"] for r in rows], dtype=float)

    def boot_ci(arr: np.ndarray) -> dict[str, float]:
        if arr.size == 0:
            return {"mean": float("nan"), "ci_low": float("nan"), "ci_high": float("nan")}
        samples = rng.choice(arr, size=(n_resamples, arr.size), replace=True)
        means = samples.mean(axis=1)
        return {
            "mean": float(arr.mean()),
            "ci_low": float(np.percentile(means, 5)),
            "ci_high": float(np.percentile(means, 95)),
        }

    out["phase_d_bootstrap"] = {
        "pf_lift": boot_ci(pf_lifts),
        "wr_lift": boot_ci(wr_lifts),
        "pnl_lift": boot_ci(pnl_lifts),
        "n_resamples": n_resamples,
        "seed": seed,
        "fold_pf_lifts": pf_lifts.tolist(),
        "fold_wr_lifts": wr_lifts.tolist(),
        "fold_pnl_lifts": pnl_lifts.tolist(),
    }
    print(json.dumps(out["phase_d_bootstrap"], indent=2, default=str))
    return out


def acceptance(out: dict[str, Any]) -> dict[str, Any]:
    rows = out.get("phase_c_holdout")
    boot = out.get("phase_d_bootstrap")
    if not rows or not boot:
        out["acceptance"] = {"ran": False, "reason": "Phase C/D did not run"}
        return out

    pf_lifts = [r["PF_lift"] for r in rows if r["PF_lift"] is not None]
    wr_lifts = [r["WR_lift"] for r in rows]
    retained = [r["retained_pct"] for r in rows if r["retained_pct"] is not None]

    median_pf_lift = float(np.median(pf_lifts)) if pf_lifts else float("nan")
    worst_pf_lift = float(min(pf_lifts)) if pf_lifts else float("nan")
    folds_pos = sum(1 for x in pf_lifts if x > 0)
    retained_ok = all(r >= 0.60 for r in retained) if retained else False
    ci = boot["pf_lift"]
    ci_excludes_zero = (ci["ci_low"] > 0) or (ci["ci_high"] < 0)

    result = {
        "median_pf_lift": median_pf_lift,
        "median_pf_lift_ok": median_pf_lift >= 0.15,
        "worst_pf_lift": worst_pf_lift,
        "worst_pf_lift_ok": worst_pf_lift >= -0.05,
        "retained_ok": retained_ok,
        "retained_per_fold": retained,
        "folds_positive_pf": folds_pos,
        "folds_positive_pf_ok": folds_pos >= 4,
        "ci_pf_lift": ci,
        "ci_excludes_zero": ci_excludes_zero,
    }
    result["all_pass"] = all([
        result["median_pf_lift_ok"],
        result["worst_pf_lift_ok"],
        result["retained_ok"],
        result["folds_positive_pf_ok"],
        result["ci_excludes_zero"],
    ])
    out["acceptance"] = result
    print("\nAcceptance check:")
    print(json.dumps(result, indent=2, default=str))
    return out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="strat_1k 5m-alignment walk-forward + bootstrap",
    )
    ap.add_argument("--start", default="2025-11-19",
                    help="ISO date or datetime (UTC) — window start. Default 2025-11-19.")
    ap.add_argument("--end", default="2026-05-21",
                    help="ISO date or datetime (UTC) — window end (exclusive). Default 2026-05-21.")
    ap.add_argument("--n-folds", type=int, default=8,
                    help="Walk-forward fold count. Default 8.")
    ap.add_argument("--test-days", type=int, default=18,
                    help="Days per holdout test slice. Default 18.")
    ap.add_argument("--symbol", default="MXF",
                    help="DB symbol label. Default MXF.")
    ap.add_argument("--filters", default="macd_hist,di_positive,above_ema20",
                    help="Comma-separated list of filter modes to sweep "
                         "(baseline=None is always added). Default = "
                         "Study 4 positive direction set.")
    ap.add_argument("--out", default="/tmp/strat_1k_5m_sweep_results.json",
                    help="JSON output path. Default /tmp/strat_1k_5m_sweep_results.json.")
    ap.add_argument("--min-retained", type=float, default=0.80,
                    help="Per-fold retention floor for filter selection. "
                         "Default 0.80. Lower (e.g. 0.30) when the baseline "
                         "is net-losing and a regime-filter is expected to "
                         "drop many trades by design.")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    global SYMBOL, FOLDS, FILTERS, MIN_RETAINED
    args = _parse_args(argv)
    SYMBOL = args.symbol
    MIN_RETAINED = args.min_retained
    start = parse_utc_date(args.start)
    end = parse_utc_date(args.end)
    FOLDS = build_folds(start, end, args.n_folds, args.test_days)

    requested = [f.strip() for f in args.filters.split(",") if f.strip()]
    unknown = set(requested) - ALL_KNOWN_FILTERS
    if unknown:
        raise SystemExit(f"unknown filter modes: {sorted(unknown)}")
    FILTERS = [None] + requested  # type: ignore[list-item]

    print(
        f"Config: symbol={SYMBOL} window={args.start}→{args.end} "
        f"n_folds={args.n_folds} test_days={args.test_days} "
        f"filters={requested}"
    )

    out: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "config": {
            "symbol": SYMBOL,
            "start": args.start,
            "end": args.end,
            "n_folds": args.n_folds,
            "test_days": args.test_days,
            "filters": requested,
        },
    }
    phase_b(out)
    phase_c(out)
    phase_d(out)
    acceptance(out)
    out["finished_at"] = datetime.now(UTC).isoformat()
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    print(f"\nResults written to {args.out}")


if __name__ == "__main__":
    main()
