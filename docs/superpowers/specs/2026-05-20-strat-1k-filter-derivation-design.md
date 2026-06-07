# strat_1k entry-filter derivation via expanded backtest + multi-method racing

**Date:** 2026-05-20
**Status:** Approved (brainstorming sign-off). Pending writing-plans.

## Context

strat_1k is a profitable TAIEX 1m LONG strategy (+399 pts / 87 trades / 47% WR over 12 live days). Exit decomposition shows TP carrying (+2005 / 35 trades / +57 avg) and TRAIL bleeding (−1712 / 45 trades / −38 avg). User wants a pre-entry veto that **keeps ≥80% of winners while killing ≥50% of losers**, ideally raising kept-WR materially above the 47% baseline.

A prior in-sample loss-pattern study (n=88, pre-bug-cleanup) yielded a candidate rule on higher-TF MA gaps but all univariate KS tests had p > 0.05, a depth-2 decision tree failed 5-fold CV, and Codex flagged selection bias from sweeping 2997 candidates. The headline +1470 pnl swing was inflated by the synthetic backfill of trade #110 (position-tracker-bug artifact). Clean baseline swing was +323. **Sample too small to commit a rule.**

This design addresses the sample-size complaint head-on: expand the dataset 6× via FinMind backfill of the prior 5 months of TXF data, run the current strat_1k spec across it via the existing backtest engine, then race three quant methods on walk-forward folds and synthesize a consensus rule.

## Goal

**Derive a single, interpretable veto predicate (or AND-combination of ≤3 predicates) for strat_1k LONG entries that maximizes net pnl on out-of-sample test folds subject to ≥80% winners retained.** Ship only if all three methods agree on the feature set and the rule generalizes to the held-out live window.

## Non-goals

- Not touching strat_1k code in this phase. Output is a *recommended rule*; user decides whether to integrate (would be a separate spec).
- Not re-deriving exit logic (TRAIL / BE / SL). Entry filter only.
- Not running on strat_1k_ai. AI variant has different exit stack and a different live calibration debate — separate work.

## Pipeline

### Stage A — FinMind backfill (~30min-2h)

- Endpoint: `POST /admin/backfill` (uses `FINMIND_TOKEN` env, already configured).
- Window: **2025-11-20 → 2026-04-21** (~5 months prior to current cagg coverage).
- Ingest path inserts into `ticks` hypertable; cagg policies auto-refresh `bars_*` views (already running).
- Front-month filter applies (`_pick_front_month`) — defends against multi-contract rollover noise.

Post-backfill sanity checks:
- `SELECT MIN(bucket), MAX(bucket), COUNT(*) FROM bars_1m WHERE symbol='MXF'` — confirm gap-free coverage.
- Spot-check a known TAIEX index event (e.g., Spring Festival close 2026-02-09 → 2026-02-13 → no bars expected those days).
- `SELECT COUNT(*) FROM ticks WHERE price < 1000` — should be 0 (PRICE_FLOOR filter).

### Stage B — Feature matrix construction

Run `app.backtest.engine.run_backtest(strategy='strat_1k', start='2025-11-20', end='2026-05-08')` (excludes held-out window). This produces a labeled trade set using CURRENT spec replayed over historical bars. Estimated ~400-600 trades.

For each trade, build the same ~30-feature matrix used in the prior loss-pattern study (`/tmp/loss_mining_features.csv` schema):
- 1m entry_ind (k, d, macd, signal, hist, plus_di, minus_di, adx) — from signal payload
- Per-resolution (5m / 10m / 15m / 30m / 1h):
  - close vs MA20 / MA60 / MA120 (sign + magnitude)
  - MACD sign + hist sign
  - EMA20 vs EMA50
  - +DI − -DI
  - ADX magnitude
  - close.diff().rolling(5).sum() / close (slope)
  - K - D (KD diff)
- Session bucket (night-open / overnight / day-open / day-mid), hour_tw
- ATR magnitude at 5m + 15m

Reuse `backend/app/indicators/service.py` for indicator computation (do NOT reimplement). Reuse `backend/app/api/routes/bars.py:VALID_RES` for resolution mapping.

### Stage C — Method race (3 subagents in parallel)

Each subagent receives:
- Feature matrix (CSV)
- Walk-forward fold definition (5 expanding-window folds)
- Hyperparameter ceiling (frozen — no grid search): `max_depth=3, min_samples_leaf=20, n_estimators=100, learning_rate=0.05, regularization C=1.0`
- Objective: **maximize sum(pnl_kept) on test fold subject to (n_winners_kept / n_winners_total) >= 0.8**

| subagent | method | output artifacts |
|---|---|---|
| #1 | Logistic regression + Lasso (sklearn) | feature coefficients, threshold for veto decision, OOS fold scores |
| #2 | Random forest + permutation importance | top features, extracted rules from top-N trees, OOS fold scores |
| #3 | XGBoost + SHAP feature attribution | model, SHAP-based rule extraction, OOS fold scores |

Each method also reports: stdev of pnl across folds (stability), and OOS-vs-IS gap (overfit detector).

### Stage D — Synthesis + held-out test

1. **Codex senior quant agent** receives all three method outputs.
2. Codex extracts the **consensus feature set** — features ranked top-10 by ALL THREE methods.
3. Codex proposes a **human-readable predicate** (e.g., `30m_close > 30m_MA20 AND 15m_MACD < 0`) using only consensus features. Sign-based or simple-threshold preferred over magnitude tunings.
4. Apply consensus rule to the held-out **2026-05-08 → 2026-05-20** live window (88 trades, never in training).
5. Report: winners_kept_pct, losers_killed_pct, kept_pnl, kept_WR, side-by-side vs baseline. If kept_pnl < baseline OR winners_kept < 70% → reject rule. If feature consensus is weak (no overlap in top-10) → no rule; investigate why and revisit data scope.

## Critical files

| File | Role |
|---|---|
| `backend/app/api/routes/backfill.py` | Stage A backfill endpoint |
| `backend/app/backtest/engine.py` | Stage B replay engine — `run_backtest`, `pair_into_trades` |
| `backend/app/indicators/service.py` | Stage B feature computation — reuse `_REGISTRY` |
| `backend/app/api/routes/bars.py` | `load_bars`, `VALID_RES` |
| `backend/app/strategies/examples/strat_1k.py` | Strategy under test (read-only) |
| `/tmp/loss_mining.py` (existing) | Prior feature build script — extend, do not duplicate |

## Deliverables

- `/tmp/backfill_summary.txt` — Stage A: rows ingested, gaps, sanity check output.
- `/tmp/backtest_trades.csv` — Stage B: trades with pnl labels.
- `/tmp/features_v2.csv` — Stage B: feature matrix.
- `/tmp/method_lr.json`, `/tmp/method_rf.json`, `/tmp/method_xgb.json` — Stage C: per-method results.
- `/tmp/consensus_rule.md` — Stage D: final rule + evidence + held-out test.

## Verification

1. Stage A: bar count post-backfill ≥ 4× current. No PRICE_FLOOR violations. Continuous-aggregate refresh succeeds.
2. Stage B: backtest produces > 300 trades. Distribution of exit_reason matches live (TP ~40%, TRAIL ~55%). If exit-mix is wildly different, investigate spec drift.
3. Stage C: each method's IS-vs-OOS gap < 30% (drops out of consideration if larger). Standard deviation of pnl across folds < 2× mean.
4. Stage D: held-out test results reported with bootstrap confidence interval (200 resamples) on kept_pnl. If 95% CI includes 0, the rule isn't proven.

## Risks

| risk | mitigation |
|---|---|
| FinMind data has gaps or rollover noise | Existing `_pick_front_month` filter + PRICE_FLOOR. Spot-check after ingest. |
| Walk-forward early folds too thin | First fold may be <100 trades; report stability across folds. |
| Regime shift between 2025-11 and 2026-05 | If OOS performance degrades systematically across folds, abandon rule and report regime instability. |
| ML methods overfit despite frozen hyperparams | Hyperparams set conservatively (max_depth=3, min_samples_leaf=20). Compare IS vs OOS — gap > 30% triggers rejection. |
| Spec drift from `8c6e56a` (5-13) | Acknowledged: backtest answers "what if current spec ran from Nov" — fair counterfactual. Held-out window is post-`8c6e56a` so it remains apples-to-apples for the rule check. |

## Out of scope (deferred)

- Implementing the rule in code (separate spec, contingent on derivation success)
- Re-deriving on a wider history (>6mo) — if 6mo proves insufficient, revisit
- Multi-side rule (LONG + SHORT) — strat_1k is LONG-only
- Exit-stack revision (TRAIL → flat SL etc.) — separate study

## Next step

After this spec is approved by user, invoke `superpowers:writing-plans` to draft the implementation plan (Stage A through Stage D execution steps).
