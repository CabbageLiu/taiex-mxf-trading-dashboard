# strat_1k Filter Derivation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Derive an interpretable pre-entry veto rule for `strat_1k` LONG entries that maximizes net pnl on out-of-sample test folds subject to ≥80% winners retained, using a 6-month FinMind-backfilled dataset and three quant methods racing in parallel (LR+Lasso, Random Forest, XGBoost).

**Architecture:** Four stages — (A) FinMind backfill 2025-11-20 → 2026-04-21; (B) backtest current `strat_1k` spec over the extended window + build feature matrix; (C) three subagents fit their respective methods on walk-forward folds; (D) Codex synthesizes a consensus rule and validates it on the held-out 2026-05-08 → 2026-05-20 live window.

**Tech Stack:** Python 3.12 / pandas / numpy / sklearn / XGBoost / FastAPI backtest engine / TimescaleDB caggs / FinMind sponsor API. No new code in `backend/`; all analysis lives in `/tmp/*.py` scripts and CSVs.

**Spec:** `docs/superpowers/specs/2026-05-20-strat-1k-filter-derivation-design.md`

---

## File structure (artifacts)

All under `/tmp/`. No git-tracked source files modified.

| Path | Stage | Producer |
|---|---|---|
| `/tmp/backfill_summary.txt` | A | curl + jq |
| `/tmp/backtest_trades.csv` | B | `/tmp/run_backtest.py` |
| `/tmp/features_v2.csv` | B | `/tmp/build_features_v2.py` |
| `/tmp/walk_forward_folds.json` | B | `/tmp/build_features_v2.py` |
| `/tmp/method_lr.json` | C | subagent #1 |
| `/tmp/method_rf.json` | C | subagent #2 |
| `/tmp/method_xgb.json` | C | subagent #3 |
| `/tmp/consensus_rule.md` | D | Codex |
| `/tmp/holdout_test.csv` | D | `/tmp/apply_rule.py` |

---

## Stage A — FinMind backfill

### Task A1: Trigger backfill 2025-11-20 → 2026-04-21

**Files:**
- Create: `/tmp/backfill_summary.txt`

- [ ] **Step 1: Confirm backend is healthy + FinMind token present**

Run:
```bash
curl -s http://127.0.0.1:8000/status | python3 -m json.tool | head -20
```
Expected: `"ok": true, "ingest_running": true`. If status shows `finmind_token: false`, stop and ask user to recreate container with `docker compose up -d --force-recreate backend`.

- [ ] **Step 2: Fire backfill request**

Run:
```bash
curl -sN -X POST 'http://127.0.0.1:8000/admin/backfill?start=2025-11-20&end=2026-04-21' \
  -H 'Content-Type: application/json' \
  --max-time 7200 \
  > /tmp/backfill_summary.txt
```
Expected: long-running (30min-2h). Will return JSON `{start, end, days[...], total_inserted, total_fetched}` when done.

- [ ] **Step 3: Verify backfill completed without error**

Run:
```bash
python3 -c "import json; d=json.load(open('/tmp/backfill_summary.txt')); print('total_inserted:', d['total_inserted']); print('error_days:', [r['day'] for r in d['days'] if r['error']])"
```
Expected: `total_inserted > 5_000_000`; `error_days: []` (or only true market-closed days).

### Task A2: Verify bar coverage post-ingest

- [ ] **Step 1: Bar-count sanity**

Run:
```bash
docker compose -f /Users/raccoon/Desktop/TAIEX/docker-compose.yml exec -T db psql -U taiex -d taiex -c \
  "SELECT MIN(bucket) AS earliest, MAX(bucket) AS latest, COUNT(*) AS bars FROM bars_1m WHERE symbol='MXF';"
```
Expected: `earliest <= 2025-11-21`, `bars >= 100000`. If `earliest > 2025-11-25`, the backfill may have skipped early dates — investigate.

- [ ] **Step 2: Continuous-aggregate refresh check**

The 5m / 10m / 15m / 30m / 1h cagg policies refresh every 30s. Trigger immediately:
```bash
docker compose -f /Users/raccoon/Desktop/TAIEX/docker-compose.yml exec -T db psql -U taiex -d taiex -c \
  "CALL refresh_continuous_aggregate('bars_5m', '2025-11-01', '2026-05-20'); \
   CALL refresh_continuous_aggregate('bars_10m', '2025-11-01', '2026-05-20'); \
   CALL refresh_continuous_aggregate('bars_15m', '2025-11-01', '2026-05-20'); \
   CALL refresh_continuous_aggregate('bars_30m', '2025-11-01', '2026-05-20'); \
   CALL refresh_continuous_aggregate('bars_1h', '2025-11-01', '2026-05-20');"
```
Note: these procs use AUTOCOMMIT — psql should accept them at top-level (no BEGIN). If you get a "cannot be executed in a transaction" error, set `-c "SET autocommit TO on;"` or run separately.

Run verify:
```bash
docker compose -f /Users/raccoon/Desktop/TAIEX/docker-compose.yml exec -T db psql -U taiex -d taiex -c \
  "SELECT '5m' AS res, COUNT(*) FROM bars_5m WHERE symbol='MXF' UNION ALL \
   SELECT '10m', COUNT(*) FROM bars_10m WHERE symbol='MXF' UNION ALL \
   SELECT '15m', COUNT(*) FROM bars_15m WHERE symbol='MXF' UNION ALL \
   SELECT '30m', COUNT(*) FROM bars_30m WHERE symbol='MXF' UNION ALL \
   SELECT '1h', COUNT(*) FROM bars_1h WHERE symbol='MXF';"
```
Expected: each row has count proportional to 1m count / resolution-multiple. (e.g., 1m=130000 → 5m≈26000 → 10m≈13000 → 1h≈2200).

### Task A3: Data quality spot-check

- [ ] **Step 1: PRICE_FLOOR violations**

Run:
```bash
docker compose -f /Users/raccoon/Desktop/TAIEX/docker-compose.yml exec -T db psql -U taiex -d taiex -c \
  "SELECT COUNT(*) FROM ticks WHERE price < 1000;"
```
Expected: `0`. If non-zero, `_pick_front_month` filter leaked — investigate `backend/app/ingest/backfill.py` and check FinMind row contract_date filter.

- [ ] **Step 2: Spot-check known market-closed days**

Run:
```bash
docker compose -f /Users/raccoon/Desktop/TAIEX/docker-compose.yml exec -T db psql -U taiex -d taiex -c \
  "SELECT DATE(bucket AT TIME ZONE 'Asia/Taipei') AS d, COUNT(*) FROM bars_1m \
   WHERE symbol='MXF' AND bucket >= '2026-02-08' AND bucket < '2026-02-15' \
   GROUP BY 1 ORDER BY 1;"
```
Expected: Spring Festival 2026 — TWSE closes 2026-02-09 → 2026-02-13. Bar count for those dates should be 0 or very low.

- [ ] **Step 3: Save coverage report**

Run:
```bash
docker compose -f /Users/raccoon/Desktop/TAIEX/docker-compose.yml exec -T db psql -U taiex -d taiex -c \
  "SELECT DATE(bucket AT TIME ZONE 'Asia/Taipei') AS d, COUNT(*) AS bars \
   FROM bars_1m WHERE symbol='MXF' GROUP BY 1 ORDER BY 1;" \
   > /tmp/bar_coverage.txt
wc -l /tmp/bar_coverage.txt
```
Expected: ~120-130 trading days. If <100, investigate gaps.

---

## Stage B — Feature matrix

### Task B1: Run backtest on extended window

**Files:**
- Create: `/tmp/run_backtest.py`
- Output: `/tmp/backtest_result.json`

- [ ] **Step 1: Write the backtest invocation script**

Create `/tmp/run_backtest.py`:
```python
import json, urllib.request

req = {
    "strategy": "strat_1k",
    "symbol": "MXF",
    "start": "2025-11-20T00:00:00+00:00",
    "end":   "2026-05-08T00:00:00+00:00",
}
data = json.dumps(req).encode("utf-8")
r = urllib.request.Request(
    "http://127.0.0.1:8000/backtest/run",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(r, timeout=1800) as resp:
    result = json.loads(resp.read())

with open("/tmp/backtest_result.json", "w") as f:
    json.dump(result, f, indent=2, default=str)

print(f"signals: {len(result['signals'])}")
print(f"trades:  {len(result['trades'])}")
print(f"stats:   {result['stats']}")
```

- [ ] **Step 2: Run it**

Run:
```bash
python3 /tmp/run_backtest.py
```
Expected: `trades: >= 300`, `stats` shows non-zero `total_pnl`, `win_rate`, etc. If trades <100, investigate strategy module reloads or data gaps.

- [ ] **Step 3: Verify exit-reason distribution matches live**

Run:
```bash
python3 -c "
import json
r = json.load(open('/tmp/backtest_result.json'))
from collections import Counter
reasons = Counter()
for t in r['trades']:
    pl = t.get('exit_payload', {}) or {}
    reasons[pl.get('exit_reason','?')] += 1
print(reasons)
"
```
Expected: TP and TRAIL each represent 30-60% of exits; matches live exit-mix (live: TP=43%, TRAIL=55%). If skewed (e.g., 90% TRAIL), spec drift between live and backtest — investigate.

### Task B2: Export trades to CSV

**Files:**
- Create: `/tmp/export_trades.py`
- Output: `/tmp/backtest_trades.csv`

- [ ] **Step 1: Write the export script**

Create `/tmp/export_trades.py`:
```python
import json, csv

r = json.load(open("/tmp/backtest_result.json"))
trades = r["trades"]

rows = []
for t in trades:
    entry = t.get("entry_payload", {}) or {}
    exit_ = t.get("exit_payload", {}) or {}
    entry_ind = entry.get("entry_ind") or {}
    rows.append({
        "trade_id": t.get("entry_signal_id"),
        "entry_ts": t["entry_ts"],
        "entry_price": t["entry_price"],
        "exit_ts": t["exit_ts"],
        "exit_price": t["exit_price"],
        "pnl_points": t["pnl_points"],
        "side": t["side"],
        "exit_reason": exit_.get("exit_reason", "?"),
        "hold_min": (t.get("hold_minutes") or 0),
        "k": entry_ind.get("k"),
        "d": entry_ind.get("d"),
        "macd": entry_ind.get("macd"),
        "signal": entry_ind.get("signal"),
        "hist": entry_ind.get("hist"),
        "plus_di": entry_ind.get("plus_di"),
        "minus_di": entry_ind.get("minus_di"),
        "adx": entry_ind.get("adx"),
    })

with open("/tmp/backtest_trades.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)

print(f"wrote {len(rows)} trades to /tmp/backtest_trades.csv")
```

- [ ] **Step 2: Run it**

Run:
```bash
python3 /tmp/export_trades.py
head -3 /tmp/backtest_trades.csv
wc -l /tmp/backtest_trades.csv
```
Expected: header + N data lines where N matches Task B1 trade count.

### Task B3: Build feature matrix v2

**Files:**
- Create: `/tmp/build_features_v2.py`
- Output: `/tmp/features_v2.csv`

- [ ] **Step 1: Dump cagg bars for full extended window**

Run:
```bash
for res in 1m 5m 10m 15m 30m 1h; do
  docker compose -f /Users/raccoon/Desktop/TAIEX/docker-compose.yml exec -T db psql -U taiex -d taiex -c \
    "\COPY (SELECT bucket, open, high, low, close FROM bars_${res} WHERE symbol='MXF' AND bucket >= '2025-11-01' AND bucket < '2026-05-21' ORDER BY bucket) TO STDOUT WITH CSV HEADER" \
    > /tmp/bars_${res}.csv
  echo "bars_${res}: $(wc -l < /tmp/bars_${res}.csv) lines"
done
```
Expected: each resolution has >1000 lines.

- [ ] **Step 2: Reuse existing feature script as starting point**

Run:
```bash
cp /tmp/loss_mining.py /tmp/build_features_v2.py
```
Then edit `/tmp/build_features_v2.py`:
- Change `TRADES_CSV = '/tmp/strat_1k_trades.csv'` → `TRADES_CSV = '/tmp/backtest_trades.csv'`
- Change `OUT_CSV = '/tmp/loss_mining_features.csv'` → `OUT_CSV = '/tmp/features_v2.csv'`
- Remove the stat / decision-tree sections (Phase B+C); we only need the feature dump.
- Ensure all 6 resolutions (1m, 5m, 10m, 15m, 30m, 1h) are loaded and features computed.

- [ ] **Step 3: Run it**

Run:
```bash
python3 /tmp/build_features_v2.py
head -1 /tmp/features_v2.csv | tr ',' '\n' | wc -l   # column count
wc -l /tmp/features_v2.csv                            # row count = trades + 1
```
Expected: ~80 columns; row count matches Task B1 trade count + 1.

- [ ] **Step 4: Sanity-check vs prior dataset**

Run:
```bash
python3 -c "
import csv
new = list(csv.DictReader(open('/tmp/features_v2.csv')))
old = list(csv.DictReader(open('/tmp/loss_mining_features.csv')))
print('new trades:', len(new))
print('old trades:', len(old))
new_cols = set(new[0].keys())
old_cols = set(old[0].keys())
print('cols only in new:', new_cols - old_cols)
print('cols only in old:', old_cols - new_cols)
"
```
Expected: new ≥ 4× old. Column diff should be empty (same schema).

### Task B4: Define walk-forward folds

**Files:**
- Create: `/tmp/walk_forward_folds.json`

- [ ] **Step 1: Generate fold definitions**

Create `/tmp/make_folds.py`:
```python
import csv, json
from datetime import datetime

rows = list(csv.DictReader(open("/tmp/features_v2.csv")))
rows.sort(key=lambda r: r["entry_ts"])

ts = [r["entry_ts"] for r in rows]
n = len(rows)
# 5 expanding-window folds. Initial train = first 40%, then expand.
# Fold i: train [0, 0.4 + 0.12*i), test [0.4 + 0.12*i, 0.4 + 0.12*(i+1))
folds = []
for i in range(5):
    train_end = int(n * (0.4 + 0.12 * i))
    test_end  = int(n * (0.4 + 0.12 * (i + 1)))
    if test_end > n:
        test_end = n
    folds.append({
        "fold": i,
        "train_idx_range": [0, train_end],
        "test_idx_range":  [train_end, test_end],
        "train_ts_range":  [ts[0], ts[train_end - 1]],
        "test_ts_range":   [ts[train_end], ts[test_end - 1]],
        "train_n": train_end,
        "test_n":  test_end - train_end,
    })

with open("/tmp/walk_forward_folds.json", "w") as f:
    json.dump(folds, f, indent=2)
for f in folds:
    print(f"fold {f['fold']}: train n={f['train_n']} → test n={f['test_n']}")
```

Run:
```bash
python3 /tmp/make_folds.py
cat /tmp/walk_forward_folds.json | python3 -m json.tool | head -50
```
Expected: 5 folds, each test fold has ≥ 30 trades.

---

## Stage C — Method race (3 parallel subagents)

### Task C1: Dispatch all 3 methods in parallel

**Files:**
- Output: `/tmp/method_lr.json`, `/tmp/method_rf.json`, `/tmp/method_xgb.json`

- [ ] **Step 1: Confirm sklearn + xgboost available in container**

Run:
```bash
docker compose -f /Users/raccoon/Desktop/TAIEX/docker-compose.yml exec -T backend uv run python3 -c "import sklearn, xgboost; print(sklearn.__version__, xgboost.__version__)"
```
Expected: version strings. If ImportError, ask user to add to `backend/pyproject.toml` dev deps and rebuild.

- [ ] **Step 2: Dispatch subagent #1 — Logistic Regression + Lasso (in background)**

Use `Agent` tool with `subagent_type="general-purpose"`, `run_in_background=True`. Prompt:

> You are a quant ML practitioner. Fit a Logistic Regression with L1 (Lasso) regularization to predict trade outcome (win=1 / loss=0) using the feature matrix at `/tmp/features_v2.csv` and walk-forward folds at `/tmp/walk_forward_folds.json`.
>
> Constraints:
> - Frozen hyperparameters: `C=1.0` (Lasso strength), `penalty='l1'`, `solver='saga'`, `max_iter=5000`, `class_weight='balanced'`, `random_state=42`.
> - Features: drop `trade_id`, `entry_ts`, `exit_ts`, `pnl_points`, `exit_reason`, `hold_min`, `side`, `entry_price`, `exit_price` (these are leakage / IDs). Use all others.
> - Standardize features (`StandardScaler`) before fitting.
> - For each fold: fit on train, predict on test, derive a veto-threshold T such that on the train set ≥80% of winners are retained, then evaluate test-set kept_pnl, kept_WR, winners_kept_pct, losers_killed_pct.
>
> Output `/tmp/method_lr.json` with keys:
> ```
> {
>   "method": "lr_lasso",
>   "hyperparams": {...},
>   "folds": [{"fold": 0, "train_kept_pnl": ..., "test_kept_pnl": ..., "test_winners_kept_pct": ..., "test_losers_killed_pct": ..., "test_kept_wr": ...}, ...],
>   "summary": {"mean_test_kept_pnl": ..., "stdev_test_kept_pnl": ..., "is_vs_oos_gap_pct": ...},
>   "top_features": [{"feature": "...", "coef": ..., "abs_coef": ...}],  // sorted by |coef|, non-zero only
>   "rule_human_readable": "VETO LONG if (a*f1 + b*f2 + ... + intercept) < T"
> }
> ```
> Reject any fold whose IS-vs-OOS gap > 30%. Print summary at end. READ-ONLY on inputs.

- [ ] **Step 3: Dispatch subagent #2 — Random Forest (in background)**

Use `Agent` tool with `subagent_type="general-purpose"`, `run_in_background=True`. Prompt:

> You are a quant ML practitioner. Fit a Random Forest classifier to predict trade outcome using the same feature matrix and folds.
>
> Frozen hyperparameters: `n_estimators=100`, `max_depth=3`, `min_samples_leaf=20`, `class_weight='balanced'`, `random_state=42`. No grid search.
>
> Same data prep + drop list + standardization-not-required (RF is scale-invariant).
>
> Output `/tmp/method_rf.json` with keys mirroring the LR output, plus:
> ```
> {
>   ...,
>   "top_features": [{"feature": "...", "importance": ..., "permutation_importance": ...}],  // sorted by permutation_importance
>   "extracted_rules": ["...human readable rule from top-3 trees..."]
> }
> ```
> Extract human-readable rules from the top 3 trees in the forest (longest path through each). Use `sklearn.tree.export_text`. Reject folds with IS-vs-OOS gap > 30%. READ-ONLY.

- [ ] **Step 4: Dispatch subagent #3 — XGBoost + SHAP (in background)**

Use `Agent` tool with `subagent_type="general-purpose"`, `run_in_background=True`. Prompt:

> You are a quant ML practitioner. Fit an XGBoost classifier to predict trade outcome.
>
> Frozen hyperparameters: `max_depth=3`, `learning_rate=0.05`, `n_estimators=100`, `subsample=0.8`, `colsample_bytree=0.8`, `min_child_weight=20`, `scale_pos_weight=(loss_count/win_count)`, `random_state=42`, `eval_metric='logloss'`. No grid search.
>
> Same data prep + drop list. Compute SHAP values via `shap.TreeExplainer` on test fold; report mean(|SHAP|) per feature for ranking.
>
> Output `/tmp/method_xgb.json` with keys mirroring the LR output, plus:
> ```
> {
>   ...,
>   "top_features": [{"feature": "...", "shap_mean_abs": ..., "shap_direction": "+ pushes toward loss / + pushes toward win"}],
>   "rule_human_readable": "...rule derived from top SHAP features + threshold..."
> }
> ```
> Reject folds with IS-vs-OOS gap > 30%. READ-ONLY.

- [ ] **Step 5: Wait for all three to complete**

All three run in parallel via `run_in_background=True`. You will receive completion notifications. Do NOT poll the JSONL transcripts. When all three notifications arrive, proceed to next step.

- [ ] **Step 6: Verify all three outputs exist + parse**

Run:
```bash
for m in lr rf xgb; do
  if [ ! -f /tmp/method_${m}.json ]; then
    echo "MISSING: /tmp/method_${m}.json"; continue
  fi
  python3 -c "
import json
r = json.load(open('/tmp/method_${m}.json'))
print('${m}:', r['summary'])
print('  top 5 features:', [f['feature'] for f in r['top_features'][:5]])
"
done
```
Expected: each method prints summary + top features. If any missing, re-dispatch.

---

## Stage D — Synthesis + held-out test

### Task D1: Codex senior quant synthesis

**Files:**
- Output: `/tmp/consensus_rule.md`

- [ ] **Step 1: Dispatch Codex via `codex:codex-rescue` subagent**

Use `Agent` tool with `subagent_type="codex:codex-rescue"`. Prompt (verbatim):

> Act as a senior quant analyst. Three ML methods raced on the same feature matrix + walk-forward folds for strat_1k LONG entry filtering. Results in `/tmp/method_lr.json`, `/tmp/method_rf.json`, `/tmp/method_xgb.json`. Spec at `docs/superpowers/specs/2026-05-20-strat-1k-filter-derivation-design.md`.
>
> Your task:
> 1. Read all three JSON outputs.
> 2. Identify the **consensus feature set** — features ranked in the top-10 by ALL THREE methods.
> 3. If consensus is empty or < 3 features: report that and stop (no rule).
> 4. Otherwise: propose a **human-readable predicate** using only consensus features. Prefer sign-based / simple-threshold over magnitude tuning. Predicate must be AND-combination of ≤3 atomic conditions.
> 5. Comment on (a) overfit risk per method (IS-vs-OOS gap), (b) regime-stability (variation across folds), (c) directional consistency (do the 3 methods agree on which features push toward loss).
> 6. Write your full analysis + rule + reasoning to `/tmp/consensus_rule.md` in markdown.
>
> Deliverable in your chat reply: <300 words summary + path to /tmp/consensus_rule.md.

- [ ] **Step 2: Verify Codex output**

Run:
```bash
ls -la /tmp/consensus_rule.md
head -50 /tmp/consensus_rule.md
```
Expected: file exists, contains a clearly-stated rule. If Codex reports "no consensus", proceed to Task D4 to report findings and stop.

### Task D2: Apply consensus rule to held-out window

**Files:**
- Create: `/tmp/apply_rule.py`
- Output: `/tmp/holdout_test.csv`

- [ ] **Step 1: Write apply script**

Create `/tmp/apply_rule.py`:
```python
"""Apply the consensus rule from /tmp/consensus_rule.md to the held-out live window.

The rule is hand-transcribed from /tmp/consensus_rule.md into the `predicate`
function below by the engineer running this plan. We do NOT auto-parse the
rule text — it's a sanity gate that a human reads it and translates to code.
"""
import csv

# === HAND-TRANSCRIBE THE PREDICATE FROM /tmp/consensus_rule.md HERE ===
def predicate(row: dict) -> bool:
    """Return True if entry should be VETOED (blocked)."""
    # Example shape:
    # return float(row["30m_close_minus_ma20_pct"]) > -0.0014 and float(row["15m_close_minus_ma60_pct"]) > -0.0009
    raise NotImplementedError("Transcribe the consensus rule from /tmp/consensus_rule.md")
# === END PREDICATE ===

# Held-out trades = original live 5-08 → 5-20 dataset (87 trades after backfill cleanup).
HOLDOUT_FEATURES = "/tmp/loss_mining_features.csv"  # built earlier from live data
rows = list(csv.DictReader(open(HOLDOUT_FEATURES)))
# Drop the synthetic backfill trade #110 (pnl=-1147 artifact).
rows = [r for r in rows if abs(float(r["pnl"]) + 1147) > 0.5]
print(f"holdout trades: {len(rows)}")

kept, vetoed = [], []
for r in rows:
    (vetoed if predicate(r) else kept).append(r)

def summarize(name, rows):
    wins = [r for r in rows if float(r["pnl"]) > 0]
    losses = [r for r in rows if float(r["pnl"]) <= 0]
    pnl = sum(float(r["pnl"]) for r in rows)
    wr = 100 * len(wins) / max(1, len(rows))
    print(f"{name}: n={len(rows)} W={len(wins)} L={len(losses)} pnl={pnl:+.0f} WR={wr:.1f}%")

summarize("BASELINE (no veto)", rows)
summarize("KEPT (post-veto)",  kept)
summarize("VETOED",            vetoed)

# Write detail CSV
with open("/tmp/holdout_test.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["vetoed"])
    w.writeheader()
    for r in rows:
        rr = dict(r); rr["vetoed"] = predicate(r)
        w.writerow(rr)

all_wins = [r for r in rows if float(r["pnl"]) > 0]
all_losses = [r for r in rows if float(r["pnl"]) <= 0]
kept_wins = [r for r in kept if float(r["pnl"]) > 0]
vetoed_losses = [r for r in vetoed if float(r["pnl"]) <= 0]
print()
print(f"winners kept: {len(kept_wins)}/{len(all_wins)} = {100*len(kept_wins)/len(all_wins):.1f}%")
print(f"losers killed: {len(vetoed_losses)}/{len(all_losses)} = {100*len(vetoed_losses)/len(all_losses):.1f}%")
```

- [ ] **Step 2: Transcribe the consensus rule into `predicate()`**

Open `/tmp/consensus_rule.md` and locate the predicate. Replace `predicate()` body in `/tmp/apply_rule.py` with the actual condition. Do NOT auto-parse — read the markdown, understand the rule, write it in Python.

- [ ] **Step 3: Run + verify**

Run:
```bash
python3 /tmp/apply_rule.py
```
Expected: prints baseline + kept + vetoed summaries with concrete numbers. winners_kept ≥ 80% AND losers_killed ≥ 50% AND kept_pnl > baseline_pnl for the rule to be considered valid.

### Task D3: Bootstrap confidence interval on kept_pnl

**Files:**
- Create: `/tmp/bootstrap_ci.py`

- [ ] **Step 1: Write bootstrap script**

Create `/tmp/bootstrap_ci.py`:
```python
import csv, random, statistics
random.seed(42)

rows = list(csv.DictReader(open("/tmp/holdout_test.csv")))
kept = [float(r["pnl"]) for r in rows if r["vetoed"] == "False"]
n = len(kept)
print(f"kept n = {n}")

samples = []
for _ in range(2000):
    s = random.choices(kept, k=n)
    samples.append(sum(s))
samples.sort()
lo = samples[int(0.025 * 2000)]
hi = samples[int(0.975 * 2000)]
mean = statistics.mean(samples)
print(f"bootstrap mean kept_pnl: {mean:+.0f}")
print(f"95% CI: [{lo:+.0f}, {hi:+.0f}]")
print(f"include zero? {'YES' if lo <= 0 <= hi else 'NO'}")
```

- [ ] **Step 2: Run**

Run:
```bash
python3 /tmp/bootstrap_ci.py
```
Expected output: bootstrap mean ≈ kept_pnl from Task D2; 95% CI bounds; whether interval crosses zero. **If CI crosses zero → rule is not statistically distinguishable from baseline; do not ship.**

### Task D4: Present findings to user

- [ ] **Step 1: Compose summary**

Write a chat message containing:
- Consensus rule (verbatim)
- Per-method top-3 features
- Walk-forward fold stability (stdev / mean ratio)
- Held-out window result table:
  | metric | baseline | post-veto |
  |---|---|---|
  | n | 87 | n_kept |
  | net pnl | +399 | … |
  | WR | 47.1% | … |
- Bootstrap 95% CI on kept_pnl
- IS-vs-OOS gap per method
- Recommendation: ship (with caveats), do not ship, or further data needed

- [ ] **Step 2: Ask user for decision**

Use `AskUserQuestion` with options:
- Implement the rule in strat_1k_ai (separate spec to be drafted)
- Keep monitoring, re-derive at 1y data
- Reject the rule (specify why)

---

## Self-review

**Spec coverage:**
- Stage A backfill → Tasks A1-A3 ✓
- Stage B feature build → Tasks B1-B4 ✓
- Stage C method race → Task C1 (dispatches 3 subagents) ✓
- Stage D synthesis + holdout → Tasks D1-D4 ✓
- Verification (bar count, exit-mix, CI crossings) embedded in each task ✓

**Placeholder scan:** All steps contain concrete commands. The `predicate()` function in Task D2 is intentionally a placeholder because it requires reading Codex's output — explicitly flagged with `NotImplementedError` and "transcribe by hand" instruction.

**Type consistency:** JSON keys for method outputs consistent across Tasks C1 / D1 / D2.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-20-strat-1k-filter-derivation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
