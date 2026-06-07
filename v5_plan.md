# V5 Plan — strategy spec compliance + trading log richness + chart polish

> Handoff document. Next session reads this top-to-bottom plus `v1_trade.md` + `v2_trade.md` and starts at Phase A.

## Context

Five intertwined asks from the user:

1. **Exit dot still missing on chart** despite the V4 retry-paint fix (commit `9da8078`). Need root-cause + actual fix.
2. **Strategy filter on chart markers** — toggle to show only `trade_strat_v1` dots, only `trade_strat_v2`, or both.
3. **Trading log on `/analysis`** must (a) carry a sequential id, (b) include the strategy that opened the trade, (c) show the strategy's indicator variables (KD / MACD / DMI) at OPEN and at CLOSE side-by-side, (d) cross-reference the chart marker's id so user can match a row to a dot.
4. **Display rename**: v1 → `30分鐘線策略`, v2 → `5分鐘策略`. Must NOT break the DB key (`trades.strategy`, `signals.strategy`, `strategy_config.name`).
5. **Live trades violate the spec** — observed live exits exceeded `tp_points = 220`. Re-read `v1_trade.md` + `v2_trade.md` and rewrite both strategies to follow them exactly.

The spec docs (read in full):

**`v1_trade.md` — 30 分鐘線策略**
- Entry on **30m** bar close, ALL must hold:
  1. `KD > 20`
  2. `MACD above 0`, with the additional qualifier *initial 3 figures of MACD trend becoming positive* (i.e. just-turned-positive — a 3-bar rising-edge condition, not a static `>0`)
  3. `+DI > 21` AND `+DI > -DI`
- Daily (1d): if ≥ 2 of the same conditions hold, "higher confidence in the position" (display only)
- Exit (ANY one):
  - Profit ≥ 220 點
  - **3m** -DI > 23
  - SL: -60 點

**`v2_trade.md` — 5 分鐘策略**
- Entry on **5m** bar close, same condition shape as v1
- Same daily-confidence display rule
- Exit (ANY one) all eval based on **1m**:
  - Profit ≥ 70 點
  - **3m** -DI ≥ 23
  - SL: -50 點

Current code drift from spec:

| concern | spec | current `trade_strat_v1.py` | current `trade_strat_v2.py` |
| --- | --- | --- | --- |
| entry resolution | 30m / 5m | 30m ✓ | 10m ✗ |
| -DI exit-assist resolution | 3m | 5m (substituted, 3m absent from RESOLUTIONS) | 2m ✗ |
| TP / SL points | 220/60 vs 70/50 | 220/60 ✓ | 220/60 ✗ |
| MACD condition | rising-edge "initial 3 positive figures" | static `> 0` ✗ | static `> 0` ✗ |
| `+DI > -DI` requirement | required | not checked ✗ | not checked ✗ |
| -DI exit threshold | v1 `>23` / v2 `≥23` | `>23` ✓ | `>23` (should be `≥23`) ✗ |
| 1m TP/SL eval (v2 only) | required | n/a | n/a — TP/SL eval lives on entry-tf bar close |

Plus: `RESOLUTIONS` / `RESOLUTION_DELTAS` / `VALID_RES` lack `3m`. No continuous aggregate `bars_3m` in DB. V4 Phase 1 added `bars_2m` + `bars_10m` (alembic `0003`). A new alembic `0004` must add `bars_3m`.

## Goals (consolidated)

1. Strategies match `v1_trade.md` / `v2_trade.md` byte-for-byte in their entry, exit-assist, and TP/SL semantics.
2. `bars_3m` continuous aggregate added; `RESOLUTIONS` / `RESOLUTION_DELTAS` / `VALID_RES` extended.
3. Display label exposed via a new `display_name: ClassVar[str]` on the `Strategy` ABC. UI renders the display label everywhere a label is shown; the DB-bound `name` ClassVar stays `trade_strat_v1` / `trade_strat_v2` so existing rows remain valid.
4. Strategy `_open_position` and `_close_position` snapshot KD / MACD / DMI values into the `Signal.payload` at both moments.
5. `PositionTracker` copies the entry+exit indicator snapshots into `trades.payload` so the analysis trading log can show "opened at K=45.3 → closed at K=52.1" deltas.
6. `/analysis` `TradesTable` gains four new columns: ID, 策略 (display name), 開倉指標, 出場指標, and uses the existing `Trade.id` as the sequential identifier.
7. Chart marker exit-dots reliably render — root-cause the post-V4 issue and apply the right fix (likely pane-relative coord clamp + visible-range projection).
8. Marker tooltip carries `#${tradeId}` chip so users can match a dot to a log row.
9. Strategy filter toggle on `/trading` TopBar (or as a small pill row) lets users hide v1, hide v2, or show both. Component state only — no lens / URL state.

## Non-goals (V5)

- Re-numbering trade ids per-strategy. The existing `Trade.id` BIGSERIAL is good enough as a globally-unique sequential id.
- Persisting filter selection across navigation.
- Multi-symbol support.
- Backtest engine changes (the engine already runs against whatever the strategy declares; rewriting the strategies suffices).

## Decisions

| decision | choice |
| --- | --- |
| trade-id source | `Trade.id` (existing `BIGSERIAL PRIMARY KEY` in `0002_trades.py:23`). No re-numbering. |
| display-name surface | New `display_name: ClassVar[str] \| None = None` on `Strategy` ABC; surfaced via `StrategyOut.display_name`. Frontend renders `display_name ?? name`. |
| 3m support | New alembic `0004_bars_3m.py` mirroring the `0003_bars_2m_10m.py:17-52` pattern. Cagg + refresh policy. |
| exit-dot fix | Read `frontend/node_modules/lightweight-charts/dist/typings.d.ts` to confirm `series.priceToCoordinate` semantics in v5; apply `chart.paneSize(0)` clamp + edge-arrow visual cue when the y falls outside the candle pane. Plus a `priceScale().getVisibleRange()` projection fallback so any out-of-range price still pins to the right edge of the candle pane. |
| filter UI | A new `MarkerFilterPills` component placed in `TopBar.tsx` after `IndicatorToggleBar`. Component state lives in `trading/page.tsx` and is passed into `<Chart>` as a new `markerStrategies?: Set<string> \| null` prop. Null = show all. |
| indicator-snapshot persistence | Strategies put `entry_ind` / `exit_ind` blobs in `Signal.payload`; `position_tracker._open_trade` and `_close` copy them into `Trade.payload.entry_ind` / `Trade.payload.exit_ind`. |
| trading-log column header language | Traditional Chinese for column headers; indicator names (KD / MACD / +DI / -DI / ADX) stay English per `CLAUDE.md` convention. |

## Phased rollout

Each phase is one PR, gated by tests + manual smoke. Inside a phase, slices A / B / C run in parallel via subagents per the project's standing workflow (see `MEMORY.md → feedback_workflow.md`). Codex review + price-tally subagent at the very end.

### Phase A — backend spec compliance (1 PR)

Slices:

- **A1**: alembic `0004_bars_3m.py` (mirror `0003_bars_2m_10m.py`), extend `RESOLUTIONS` + `RESOLUTION_DELTAS` (`backend/app/ingest/runner.py:20-32`), extend `VALID_RES` (`backend/app/api/routes/bars.py:16`). Tests for `_bucket_start("3m")` boundary cases.

- **A2**: rewrite `backend/app/strategies/examples/trade_strat_v1.py`:
  - `resolutions = ["3m", "30m", "1d"]` (was `["5m", "30m", "1d"]`).
  - dispatch `if ev.resolution == "3m"` → `_exit_assist`.
  - new helper `_macd_just_turned_positive(macd_series)` returning true iff the last bar is the first of a 3-bar streak above zero (`macd[-3] <= 0 and macd[-2] > 0 and macd[-1] > macd[-2]`). Replace the static `macd_curr > 0` check.
  - add `+DI > -DI` to the LONG entry gate; symmetric `-DI > +DI` to the SHORT gate.
  - exit threshold stays `> 23`.
  - TP / SL stay 220/60.
  - `display_name: ClassVar[str] = "30分鐘線策略"`.
  - `_open_position` payload already carries `entry: {k, d, macd, di}`. Extend with `entry_ind: {k, d, macd, signal, hist, plus_di, minus_di, adx}` so the trading log has full snapshot. Keep `entry` key for backwards compat with existing test fixtures.
  - `_close_position` payload gains `exit_ind: {...same shape...}` computed from the indicator series at the exit bar's index.
  - cooldown stays 5 × 30m bars.

- **A3**: rewrite `backend/app/strategies/examples/trade_strat_v2.py`:
  - `resolutions = ["1m", "3m", "5m", "1d"]`
  - dispatch:
    - `if ev.resolution == "5m"` → `_on_entry` (entry/TP/SL eval; rename current `_on_10m`)
    - `if ev.resolution == "3m"` → `_exit_assist` (-DI ≥ 23)
    - `if ev.resolution == "1m"` → `_check_tp_sl_minute` (TP/SL only, no entry logic)
    - `1d` → daily confidence
  - `tp_points: float = 70.0`, `sl_points: float = 50.0` defaults
  - `exit_di_threshold: float = 23.0`; use `>=` instead of `>` to honor v2 spec
  - cooldown 5 × 5m bars (parameterized via `cooldown_bars=5`; semantic stays "5 × entry-tf bars")
  - `display_name: ClassVar[str] = "5分鐘策略"`
  - same MACD rising-edge + `+DI > -DI` rules as v1
  - same `entry_ind` / `exit_ind` payload shape

  Tests: cover the new MACD rising-edge gate (positive case + 3 false-positives where it must NOT fire), the `+DI > -DI` gate, the 1m TP/SL eval (separate code path from entry-tf), and v2-specific TP=70 / SL=50.

- **A4**: `backend/app/strategies/base.py` adds `display_name: ClassVar[str] | None = None`. `backend/app/api/routes/strategies.py` `StrategyOut` adds `display_name: str | None`, populated from `cls.display_name`.

- **A5**: `backend/app/runner/position_tracker.py` `_open_trade` / `_close` copy `signal.payload.entry_ind` / `signal.payload.exit_ind` into `trade.payload["entry_ind"]` / `trade.payload["exit_ind"]`. Tests assert the JSONB column carries the snapshots end-to-end.

Sync gate: full pytest green (target ~155 tests after A's additions).

### Phase B — chart marker fix + trade-id chip + strategy filter (1 PR)

Slices:

- **B1**: exit-dot rendering fix in `frontend/components/Chart.tsx`. Investigate (read `node_modules/lightweight-charts/dist/typings.d.ts` for `priceToCoordinate` semantics). Apply: (a) clamp y into the candle pane via `chart.paneSize(0).height`; (b) when clamped, paint a small edge arrow (▲ if exit price was above the visible range, ▼ if below) so the user knows direction; (c) keep the 250 ms retry from `9da8078`. Use `window.__taiexMarkerStats` to confirm by browser console after the fix lands.
- **B2**: `frontend/components/TradeMarkerTooltip.tsx` gains a `#${event.tradeId}` chip in the head row, before the kind chip. New CSS rule `.trade-marker-id` mirroring `.trade-marker-strategy` with sumi-gold accent.
- **B3**: new `frontend/components/MarkerFilterPills.tsx` — three pills `全部` / `30分` / `5分` (or strategy display names). Mounts in `frontend/components/TopBar.tsx` after the indicator pills. Local component state owned by `frontend/app/trading/page.tsx`, passed through as a new `markerStrategies` prop on `<Chart>`. Chart's `tradeEvents` memo filters by this prop before pushing to the canvas. Initial state = "全部".

Sync gate: tsc + build clean. Manual smoke: open `/trading`, see exit dots; toggle filter pills, see only one strategy.

### Phase C — trading log columns + display-name UI (1 PR)

Slices:

- **C1**: extend `frontend/lib/api.ts` `Trade` type — payload remains `Record<string, unknown>` but document the new `entry_ind` / `exit_ind` shape via a discriminated `TradeIndicators` type that downstream consumers cast to.
- **C2**: `frontend/components/TradesTable.tsx` adds four columns:
  - `編號` (`tr.id`)
  - `策略` (strategy display name — fetch via the existing `useStrategies` hook to map `name → display_name`; small mapping memo in the component)
  - `開倉指標` (KD / MACD / +DI / -DI rendered compactly e.g. `K54 D51 / MACD+9 / +DI33 -DI19`)
  - `出場指標` (same shape, when present)
  Reuses `tabular-nums` and existing `.trades-table` CSS. Header copy in TC. Indicator names stay English.
- **C3**: `frontend/components/StrategySelector.tsx` and `MarkerFilterPills.tsx` render `display_name ?? name`. Search/filter still uses `name` for cache stability. Confirm the existing `frontend/components/AlertLog.tsx` and `frontend/components/DailyConfidenceBadge.tsx` follow the same fallback.

Sync gate: tsc + build. Manual smoke: trade row shows ID + 策略 + indicator deltas; chart dot's tooltip shows matching `#id`.

### Final review — price-tally + codex-rescue (parallel)

Same standing workflow:

- **price-tally subagent**: audit every price/number surface — chart tooltip, hi/lo badge, trade-marker tooltip, KPI cards, AI insight payload, alert log — for source consistency, decimal formatting, timezone uniformity, `PRICE_FLOOR`. Must include the new indicator snapshot rendering in the trading log.
- **codex-rescue subagent**: review the full V5 diff for correctness, prompt-cache invariants (no insights changes here, so should be a no-op), and any regression against V4's existing behavior.

## Critical files

### Backend

- `backend/app/db/migrations/versions/0004_bars_3m.py` (NEW) — mirror of `0003_bars_2m_10m.py`.
- `backend/app/ingest/runner.py:20-32` — `RESOLUTIONS` + `RESOLUTION_DELTAS`.
- `backend/app/api/routes/bars.py:16` — `VALID_RES`.
- `backend/app/strategies/base.py` — add `display_name: ClassVar[str] | None`.
- `backend/app/strategies/examples/trade_strat_v1.py` — full rewrite per spec.
- `backend/app/strategies/examples/trade_strat_v2.py` — full rewrite per spec (resolutions, TP, SL, dispatch, exit threshold, MACD rule, +DI > -DI rule, display_name).
- `backend/app/api/routes/strategies.py:17-23, 51-57` — `StrategyOut.display_name`.
- `backend/app/runner/position_tracker.py` — copy `entry_ind` / `exit_ind` into `Trade.payload` at open/close.
- `backend/tests/test_trade_strat_v1.py`, `backend/tests/test_trade_strat_v2.py` — extend with MACD rising-edge cases, `+DI > -DI` cases, payload-snapshot assertions.
- `backend/tests/test_resolutions_2m_10m.py` (existing) → either rename or add `test_resolutions_3m.py` mirroring the same shape.

### Frontend

- `frontend/components/Chart.tsx` — exit-dot fix, `markerStrategies` prop wiring, filter applied in `tradeEvents` memo.
- `frontend/components/TradeMarkerTooltip.tsx` — `#${event.tradeId}` chip.
- `frontend/components/MarkerFilterPills.tsx` (NEW).
- `frontend/components/TopBar.tsx` — host the new pills.
- `frontend/components/TradesTable.tsx` — four new columns.
- `frontend/lib/api.ts` — `StrategyOut.display_name`, `TradeIndicators` shape doc.
- `frontend/app/globals.css` — `.trade-marker-id` style + any new pill styling.
- `frontend/app/trading/page.tsx` — own the `markerStrategies` state.
- `frontend/components/StrategySelector.tsx`, `frontend/components/AlertLog.tsx`, `frontend/components/DailyConfidenceBadge.tsx` — render `display_name ?? name`.
- `frontend/lib/i18n.ts` — column headers `編號 / 策略 / 開倉指標 / 出場指標`, filter pills `全部 / 30分 / 5分` (or just reuse strategy display names).

## Reuse — do not duplicate

- `Trade.id` is the sequential identifier — do NOT add a parallel id.
- `Strategy.params_schema` is already exposed via `StrategyOut`; `display_name` follows the same pattern.
- `useTrades` query in `frontend/lib/queries.ts` already returns `payload`; `TradesTable` just needs to read it.
- `fmtPrice` / `tabular-nums` in `TradesTable.tsx` — reuse for indicator deltas.
- The price-tally + codex-rescue review pattern — same as V4 closeout.

## Verification

Backend:

1. `docker compose down -v && docker compose up -d --build` — alembic 0004 runs cleanly, `\dv bars_3m` exists in psql.
2. `cd backend && uv run pytest -q` — full suite green, target ~155 tests after A.
3. After live ingest accumulates a few 30m and 5m closes, observe the strategy loop firing entries that match all four conditions (KD>20, MACD just-turned-positive, +DI>21 AND +DI>-DI, on bar close).
4. Force a TP / SL / -DI flip exit in dev (synthetic ticks via `wipe_and_rebackfill.py` + a hand-crafted price series in tests) and confirm the exit fires per spec — NOT after the price has already moved 250+ pts.
5. Inspect `trades.payload` JSONB after a closed trade — assert `entry_ind` and `exit_ind` are populated with KD / MACD / DMI snapshots.

Frontend:

1. `npx tsc --noEmit && npm run build` clean across all phases.
2. `/trading?s=trade_strat_v1`: chart shows a dot for entry AND a dot for exit. Hover the exit dot — tooltip shows `#1` chip, kind `關倉`, side `多`, price, PnL, reason. (If exit price was outside the visible candle range, see edge arrow.)
3. Marker filter pills: clicking `30分` hides v2 dots; clicking `5分` hides v1 dots; `全部` shows both.
4. `/analysis`: trades table shows `#`, `策略 = 30分鐘線策略`, `開倉指標 = K54 D51 / MACD+9 / +DI33 -DI19`, `出場指標 = K… D… / MACD… / +DI… -DI…`. Hover the chart dot for the same trade — `#id` matches the row.
5. Strategy selector dropdown shows the display name only, raw `name` on hover via `title` attr.
6. AI insight (if `ANTHROPIC_API_KEY` set): the prompt should still receive `strategy = "trade_strat_v1"` (the canonical key, not the display name) so cache fingerprints don't collide.

End-to-end smoke: walk the full happy path on a live `docker compose up` with both strategies enabled, observe a real trade open + close, verify chart + analysis + insight all reflect the same trade row.

## Risks

- **3m cagg storage cost** — like 2m / 10m, 3m's bucket math is sane but row count grows linearly. Same mitigation as Phase 1's `bars_2m` note: monitor compress_segmentby behavior; if costly, demote `bars_3m` to a plain view over `bars_1m`.
- **MACD rising-edge false positives** — spec phrasing "initial 3 figures of MACD trend becoming positive" is ambiguous; chosen interpretation is "MACD just crossed above zero with the last 2-3 bars showing rising momentum." Tests must lock the exact rule (3-bar window: `macd[-3] <= 0 and macd[-2] > 0 and macd[-1] > macd[-2]`) so future drift is caught.
- **Display name in DB vs UI mismatch** — if a developer ever uses the display name as a query key, things break silently. Ensure backend never accepts display name as input on `/strategies/{name}/*` routes.
- **Exit-dot pane offset assumption** — pane 0 is the candle pane today. If a future refactor adds a pane above pane 0 (unlikely), the `chart.paneSize(0)` math breaks. Add a defensive check + fallback to `series.priceToCoordinate` direct.
- **Trades.payload shape evolution** — older closed trades pre-Phase-A have `payload = {}`. The trading log must render `—` for missing snapshots, not crash. Confirm in test.
- **Strategy filter pills + lens interaction** — when lens is active and `compare=1`, filter pills should still gate which markers render; design so the filter wins over the lens (filter is "show me only this strategy"; if filter excludes lens.strategy, no dots paint).

## Out of scope (V6)

- Real auth (multi-user).
- Per-strategy color customization in user prefs.
- Per-trade fees / slippage / position sizing.
- More than two strategies on the chart simultaneously (current `STRATEGY_COLORS` map covers exactly v1/v2; if V6 adds v3, extend the palette).
- Strategy lifecycle versioning (when v1 changes spec mid-season, history queries should still surface old behavior).
- `next_bar_open` fill mode in backtest engine.

## Handoff for the next session

The next session should:

1. Read this file in full as the authoritative spec.
2. Read `v1_trade.md` and `v2_trade.md` in full as the strategy source-of-truth.
3. Start with Phase A — backend changes — since frontend phases depend on the new payload shape and `display_name` field.
4. Commit boundary at end of each phase. Tests + ruff clean before promoting.
5. End with the price-tally + codex-rescue review pair before merging.
