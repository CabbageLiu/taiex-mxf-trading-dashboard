"""Smoke tests for ``scripts.permutation_inference``.

Verifies the fold builder produces well-formed expanding-window folds and
the blocked permutation test returns sensible p-values on simple inputs.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts.permutation_inference import (
    blocked_permutation_pvalue,
    build_folds,
    parse_utc_date,
)


def test_build_folds_182d_8folds():
    """Plan 182-day window split into 8 × 18-day test slices.

    Note: ``2025-11-19 → 2026-05-21`` is 183 calendar days end-exclusive
    (the plan's "182 days" headline rounds the inclusive count). The fold
    builder consumes ``(end - start).days``, so initial train works out
    to ``183 - 8*18 = 39`` days — close enough to the plan's "38 day"
    estimate, with the same 8-fold expanding structure intact.
    """
    start = datetime(2025, 11, 19, tzinfo=UTC)
    end = datetime(2026, 5, 21, tzinfo=UTC)
    folds = build_folds(start, end, n_folds=8, test_days=18)
    assert len(folds) == 8
    total_days = (end - start).days
    expected_initial_train = total_days - 8 * 18
    name0, tr_s0, tr_e0, te_s0, te_e0 = folds[0]
    assert name0 == "fold0"
    assert tr_s0 == start
    assert (tr_e0 - tr_s0).days == expected_initial_train
    assert (te_e0 - te_s0).days == 18
    # Last fold's test range ends at the window end.
    name7, _, tr_e7, _, te_e7 = folds[7]
    assert name7 == "fold7"
    assert (tr_e7 - start).days == expected_initial_train + 7 * 18
    assert te_e7 == end


def test_build_folds_150d_7folds():
    """Study-5 strict 150-day window with 7 × 18-day test slices."""
    start = datetime(2025, 11, 19, tzinfo=UTC)
    end = datetime(2026, 4, 20, tzinfo=UTC)
    folds = build_folds(start, end, n_folds=7, test_days=18)
    assert len(folds) == 7
    # Initial train = 152 - 126 = 26 days for this exact range.
    # (build_folds derives initial train = (end-start).days - n_folds*test_days.)
    total_days = (end - start).days
    expected_initial_train = total_days - 7 * 18
    assert (folds[0][2] - folds[0][1]).days == expected_initial_train
    # Folds chain contiguously.
    for i in range(1, len(folds)):
        assert folds[i][1] == folds[0][1]  # train_start is fixed (expanding)
        assert folds[i][2] == folds[i - 1][2] + (folds[i - 1][4] - folds[i - 1][3])


def test_build_folds_rejects_too_short_window():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 10, tzinfo=UTC)
    with pytest.raises(ValueError):
        build_folds(start, end, n_folds=5, test_days=5)


def test_blocked_permutation_strong_positive():
    """All-positive lifts → small one-sided p (reject H0)."""
    out = blocked_permutation_pvalue(
        [0.8, 0.7, 0.9, 1.1, 0.6, 0.8, 1.0, 0.7],
        n_iter=2000,
        seed=42,
    )
    assert out["n_blocks"] == 8
    assert out["observed_median"] > 0.5
    # With 8 same-sign blocks, P(median≥obs) ≤ 1/256 ≈ 0.004.
    assert out["p_one_sided_greater"] < 0.05


def test_blocked_permutation_zero_centered():
    """Symmetric lifts around zero → large one-sided p (cannot reject)."""
    out = blocked_permutation_pvalue(
        [0.2, -0.3, 0.1, -0.1, 0.2, -0.2, 0.1, -0.1],
        n_iter=2000,
        seed=42,
    )
    assert out["p_one_sided_greater"] > 0.10
    assert 0.0 < out["p_two_sided"] <= 1.0


def test_blocked_permutation_deterministic_under_seed():
    lifts = [0.1, 0.2, -0.05, 0.3, -0.1]
    a = blocked_permutation_pvalue(lifts, n_iter=1000, seed=42)
    b = blocked_permutation_pvalue(lifts, n_iter=1000, seed=42)
    assert a == b


def test_blocked_permutation_empty_input():
    out = blocked_permutation_pvalue([], n_iter=500, seed=42)
    assert out["n_blocks"] == 0
    assert out["p_two_sided"] == 1.0
    assert out["p_one_sided_greater"] == 1.0


def test_parse_utc_date_iso_date():
    dt = parse_utc_date("2026-05-21")
    assert dt == datetime(2026, 5, 21, tzinfo=UTC)


def test_parse_utc_date_iso_datetime():
    dt = parse_utc_date("2026-05-21T00:00:00Z")
    assert dt == datetime(2026, 5, 21, tzinfo=UTC)
