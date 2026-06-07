"""Blocked permutation inference + walk-forward fold builder.

Phase 0 helpers (smooth-sniffing-meadow plan, Subagent A). Used by the
182-day study scripts (sweep_5m_alignment, exit_path_pathology,
loss_heatmap_25cell) and consumed by Phase 2 subagents B/C/D/E.

Two pieces:

1. ``build_folds(start, end, n_folds, test_days)`` — expanding-window
   walk-forward fold builder. Returns a list of
   ``(name, train_start, train_end, test_start, test_end)`` tuples.
   Designed for any contiguous date range; the prior hard-coded April-May
   fold structure is replaced by a parametric builder so the same scripts
   work on the 182-day window AND the strict 150-day Study-5 window.

2. ``blocked_permutation_pvalue(observed_lifts, n_iter=5000, seed=42)``
   — primary statistical gate per codex Phase 1 audit. Standard i.i.d.
   bootstrap is invalid under trade-cooldown clustering + regime overlap;
   block-permutation (sign-flip on each fold-block) handles the
   dependence structure correctly.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import numpy as np

FoldRow = tuple[str, datetime, datetime, datetime, datetime]


# --------------------------------------------------------------------------- #
# Walk-forward fold builder                                                   #
# --------------------------------------------------------------------------- #

def build_folds(
    start: datetime,
    end: datetime,
    n_folds: int,
    test_days: int,
) -> list[FoldRow]:
    """Build ``n_folds`` expanding-window walk-forward folds.

    Convention (matches the original April-May April-May April-May April-May
    fold structure used by Subagent B/C analyses):

      * The window ``[start, end)`` is split into a single trailing TEST
        region of size ``n_folds * test_days`` and an initial TRAIN region
        equal to the remainder.
      * Fold ``k ∈ [0, n_folds)`` has:
            train = [start, start + initial_train + k * test_days)
            test  = [train_end, train_end + test_days)
      * Initial train size therefore = ``total_days - n_folds * test_days``.

    For the 182-day full window with n_folds=8, test_days=18 → initial
    train = 38 days, growing to 164 days in fold 7.

    For the 150-day strict window with n_folds=7, test_days=18 → initial
    train = 24 days, growing to 132 days in fold 6.

    Both ends are half-open (test_start inclusive, test_end exclusive).
    Caller is responsible for symbol + UTC awareness; this function only
    performs date arithmetic.
    """
    if n_folds <= 0:
        raise ValueError("n_folds must be positive")
    if test_days <= 0:
        raise ValueError("test_days must be positive")
    total = (end - start).days
    if total <= n_folds * test_days:
        raise ValueError(
            f"window {start.date()}→{end.date()} ({total}d) too short for "
            f"{n_folds} folds × {test_days}d test slices"
        )

    initial_train_days = total - n_folds * test_days
    folds: list[FoldRow] = []
    for k in range(n_folds):
        train_start = start
        train_end = start + timedelta(days=initial_train_days + k * test_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        folds.append((f"fold{k}", train_start, train_end, test_start, test_end))
    return folds


# --------------------------------------------------------------------------- #
# Blocked permutation                                                         #
# --------------------------------------------------------------------------- #

def blocked_permutation_pvalue(
    observed_lifts: Iterable[float],
    n_iter: int = 5000,
    seed: int = 42,
) -> dict[str, float | int]:
    """Block-sign-flip permutation p-value for H0: median lift = 0.

    Each fold's lift is treated as one block. Under H0, the sign of each
    block is exchangeable (symmetry around zero). The null distribution is
    constructed by, on each iteration, drawing an independent ±1 sign per
    block and recording the median of the signed lifts.

    Returns ``{"observed_median", "n_blocks", "n_iter", "p_two_sided",
    "p_one_sided_greater"}``.

    - ``p_one_sided_greater``: P(null_median >= observed_median) — the
      relevant test when the analyst pre-registered "lift > 0" as H1
      (e.g. Study 5 inverted-direction shipping gate).
    - ``p_two_sided``: 2 * min(P(null >= obs), P(null <= obs)) — reported
      for completeness / sensitivity.

    Caller is responsible for choosing the correct tail. The function is
    deterministic given ``seed``; rerunning with the same observed_lifts
    + same seed returns identical p-values.

    Empty input or all-zero observed lifts: returns p_two_sided=1.0,
    p_one_sided_greater=1.0 (cannot reject H0).
    """
    obs = np.asarray(list(observed_lifts), dtype=float)
    obs = obs[~np.isnan(obs)]
    n = obs.size
    if n == 0:
        return {
            "observed_median": float("nan"),
            "n_blocks": 0,
            "n_iter": n_iter,
            "p_two_sided": 1.0,
            "p_one_sided_greater": 1.0,
        }

    observed_median = float(np.median(obs))

    rng = np.random.default_rng(seed)
    # Each row of `signs` is one permutation: n independent ±1 draws.
    signs = rng.choice([-1.0, 1.0], size=(n_iter, n))
    null_medians = np.median(signs * obs[np.newaxis, :], axis=1)

    # +1 in the numerator → Lehmann's mid-p correction for ties (standard).
    p_greater = float((np.sum(null_medians >= observed_median) + 1) / (n_iter + 1))
    p_less = float((np.sum(null_medians <= observed_median) + 1) / (n_iter + 1))
    p_two = float(min(1.0, 2.0 * min(p_greater, p_less)))

    return {
        "observed_median": observed_median,
        "n_blocks": n,
        "n_iter": n_iter,
        "p_two_sided": p_two,
        "p_one_sided_greater": p_greater,
    }


# --------------------------------------------------------------------------- #
# Convenience: parse plan-standard date strings                               #
# --------------------------------------------------------------------------- #

def parse_utc_date(s: str) -> datetime:
    """Parse YYYY-MM-DD or ISO datetime; return a UTC-aware datetime."""
    if "T" in s:
        # Allow trailing Z.
        s_clean = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s_clean)
    else:
        y, m, d = s.split("-")
        dt = datetime(int(y), int(m), int(d), tzinfo=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
