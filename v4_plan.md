# V4 Plan — Strategy as the unit of analysis + working alert plumbing

## Context

V3 / V3.5 shipped `trade_strat_v1`, a backtest engine, and a separate `/backtest` page. This is the wrong shape. The user model is:

> "I pick a strategy and a chart window. Everything I see — chart overlays, KPI strip, trade history, AI insight — should be that strategy's behavior on that window."

The current state breaks this:

- `/trading` shows live signal markers only. No strategy-driven trades on the chart.
- `/analysis` reads the live `trades` DB table. It cannot show what a strategy *would* have done across history.
- `/backtest` is a third destination that duplicates the analysis layout.
- Strategy selector lives only on `/trading`'s `TopBar` and doesn't propagate cleanly. The right rail panels are isolated lists.
- 即時訊號 and 通知遞送 panels render but the underlying wiring (Discord / n8n / persistence on reload) is partial. The UI doesn't tell the user whether channels are configured or whether deliveries are succeeding.

V4 collapses this into a single shape: **strategy + window** is the global lens, and `即時訊號` + `通知遞送` become first-class observable surfaces.

## Goals

1. Strategy selector lives in the `/trading` right rail alongside `即時訊號` and `通知遞送`.
2. Selecting a strategy + window runs a backtest. Backtest trades + signals render on the price chart as markers (entry/exit) + dashed connector lines, scoped to the visible chart range.
3. `/analysis` KPI strip and trades table reflect the *selected strategy's backtest* over the visible window — not the raw live `trades` table.
4. AI insight (`POST /insights/strategy`) takes the backtest KPI + trade log as its source of truth.
5. `即時訊號` persists across reload, surfaces strategy attribution, and survives WS reconnect.
6. `通知遞送` reflects real Discord / n8n / inapp delivery status, with a clear configured-vs-unconfigured indicator and a manual "test webhook" affordance.
7. Retire `/backtest` as a top-level route; its content lives where the user already is.

## Non-goals (V4)

- Multi-strategy comparison on one chart.
- Sharing backtest results across users (no auth yet).
- Tick-level intraday TP/SL replay (still bar-close granularity).
- Webhook templating / per-channel payload customization.

## Architecture changes

### 1. Strategy + window as URL state

Promote three query params to a shared global lens consumed by both `/trading` and `/analysis`:

| param | meaning | default |
| --- | --- | --- |
| `s` | strategy name | none (lens off) |
| `start` | ISO date / timestamp | derived from chart window or last 7 days |
| `end` | ISO date / timestamp | now (chart edge) |

When `s` is unset, both pages fall back to the V3 behavior (live data only). When set, both pages render through the backtest lens.

A new `frontend/lib/lens.ts` provides:

- `useLens()` → `{strategy, start, end, setStrategy, setRange, isActive}` reading from `useSearchParams` and `useRouter` for writes.
- Persists a small "last lens" object in `localStorage` so reopening the dashboard restores the prior selection.

### 2. Single `useBacktest` query (not mutation)

`useBacktest()` becomes a TanStack `useQuery`, keyed on `[strategy, start, end, params_hash]`, `enabled: lens.isActive`. The mutation form goes away.

Manual "rerun" button invalidates the key. `staleTime: 60_000` so navigating between `/trading` and `/analysis` reuses the cache.

### 3. Backend extensions

#### `POST /backtest/run` → `GET /backtest/run` with cached body

Backtests are deterministic given `(strategy, params, symbol, start, end, code_version)`. Add an in-process LRU keyed on this fingerprint. POST stays for params overrides; for the common case the frontend sends a deterministic GET-style query and gets a cached hit.

#### `POST /insights/strategy` accepts inline trade payload

Today the route queries the live `trades` table. Extend the request body:

```json
{
  "strategy": "...",
  "start": "...",
  "end": "...",
  "filter": "all|win|loss",
  "trades": [BacktestTrade...],   // optional; if present, used in lieu of DB query
  "stats": BacktestStats           // optional; computed if absent
}
```

When `trades` is provided, the backend skips the DB read and threads the inline payload into the prompt. This keeps the prompt-injection JSON-encoding guard intact (per CLAUDE.md the user payload is JSON-encoded, not f-string interpolated).

Cache key fingerprint already hashes `(trade_id, pnl_points)`; extend to optionally accept a content hash so two distinct strategies with the same trades don't collide.

#### `GET /signals` (new)

Generic recent-signals endpoint, paginated by ts. Replaces the per-strategy `/strategies/{name}/signals` for the right-rail panel. Filters: `strategy?`, `since?`, `limit`. Used to seed `即時訊號` on mount before WS catches up.

#### `GET /alerts` already exists — extend with `channel?` filter and `success?` filter

Surface in `/alerts/stats` (new): `{ discord: {sent, failed, last_ts}, n8n: {...}, inapp: {...} }`. Powers the channel health indicators.

#### `POST /admin/test-webhook` (new)

Body: `{channel: "discord"|"n8n"}`. Fires a synthetic `Signal` payload through the configured webhook. Used by the `通知遞送` panel's "測試發送" button. Returns the delivery result (success + status + body excerpt). Auth-gated stub for V4 (single-user); real auth deferred to V5.

### 4. Frontend layout changes

#### `/trading`

Right rail (`<aside class="trading-side">`) order top→bottom:

1. **`StrategyLensControl`** (new, ~150 LOC) — strategy combobox (reuses `StrategySelector`'s combobox shell), date-range picker (start, end with quick presets: 1D / 1W / 1M / 全部), 執行 button, `重置` (clears lens). Surfaces backtest summary stats inline (trade count, net pnl) once results are in.
2. **`DailyConfidenceBadge`** — moved out of the chart overlay into the rail. Visible only when the active strategy is `trade_strat_v1` (or any strategy that exposes `dump_state`).
3. **`即時訊號`** panel (existing AlertLog top half) — extended:
   - Seeded from `GET /signals?limit=50` on mount.
   - WS appends; signals dedupe by id.
   - Per-row: ts (TPE), strategy (badge), side glyph (TW colors), price, reason. Click → scroll chart to that bar (uses `chart.timeScale().scrollToPosition`).
   - Empty state: "尚無觸發訊號".
4. **`通知遞送`** panel (existing AlertLog bottom half) — extended:
   - Channel chips at top (`discord` / `n8n` / `inapp`) with health dot: green (configured + last delivery within 24h success), amber (configured but failures recent), grey (not configured).
   - Polls `/alerts/stats` every 30s.
   - "測試發送" button per configured channel → `POST /admin/test-webhook`.
   - Recent attempts list below (existing).

Chart overlay logic in `Chart.tsx`:

- Today: WS-streamed `signal` messages render markers/lines.
- V4: ALSO render markers + connector lines from the backtest result. Use a different visual treatment so backtest "what-if" markers are visually distinct from live "actually fired" markers — backtest gets dotted connector + hollow shape, live gets dashed connector + filled shape.
- Hide live markers entirely when the lens is on a non-default time window — they reference signals from outside the displayed window and would mislead.

#### `/analysis`

Driven by `useLens()`:

- Lens active: KPI strip pulls from `useBacktest().stats`. Trades table pulls from `useBacktest().trades`. Header subtitle says "回測：{strategy} · {start}–{end}".
- Lens off: existing behavior — `useTradeStats()` / `useTrades()` from the live `trades` DB table.

`TradeFilterBar` retains its `result=win|loss|all` filter, applied client-side to the lensed trade list when active.

`TradeInsightPanel`:

- Lens active: `生成洞察` posts the backtest payload inline to `/insights/strategy`. Header reads "AI 回測洞察 · {strategy}".
- Lens off: existing behavior.
- Cache key already hashes `(trade_id, pnl_points)`; the inline trades have synthetic ids, but the same fingerprint logic applies.

#### Retire `/backtest` route

Convert `app/backtest/page.tsx` to a redirect to `/analysis?s=<previous selection>` so old bookmarks still resolve. Remove from `ShellHeader` nav.

### 5. Data flow

```
URL ?s=&start=&end=
   │
   ▼
useLens() ────────┬─► useBacktest({s,start,end})  TanStack Query, 60s stale
                  │           │
                  │           ▼
                  │       GET /backtest/run (cached, fingerprint LRU)
                  │
                  ├─► /trading
                  │     ├─ StrategyLensControl (form + summary)
                  │     ├─ Chart overlays (backtest markers/lines)
                  │     ├─ Live Signals (WS + GET /signals)
                  │     └─ Alert Delivery (GET /alerts/stats + POST test-webhook)
                  │
                  └─► /analysis
                        ├─ KPI strip ← stats from useBacktest
                        ├─ Trades table ← trades from useBacktest
                        └─ Insight panel ← POST /insights/strategy {trades, stats}
```

## Phased implementation

### Phase 1 — backend lens primitives (1 PR)

- `frontend/lib/lens.ts` skeleton (no UI yet; types only).
- `GET /backtest/run` cache layer + LRU.
- `POST /insights/strategy` accepts optional inline `trades` / `stats`.
- `GET /signals` endpoint.
- Tests: cache hit/miss, inline-trades insight path, signal pagination.

### Phase 2 — `/trading` right rail (1 PR)

- New `StrategyLensControl` component.
- Move `DailyConfidenceBadge` from chart overlay into rail.
- Extend `Chart.tsx` to render backtest markers + connectors with the dotted/hollow visual treatment.
- Hide live markers when lens window is non-default.

### Phase 3 — `/analysis` lens integration (1 PR)

- `useLens` consumed by KPI strip + trades table + insight panel.
- Conditional: lens off → live; lens on → backtest.
- Insight panel posts inline payload.

### Phase 4 — alert plumbing (1 PR)

- `GET /alerts/stats` endpoint.
- `POST /admin/test-webhook` endpoint.
- Channel chips + 測試發送 button.
- 即時訊號 mount-time seed via `GET /signals`.
- 即時訊號 row click → `chart.timeScale().scrollToPosition`.

### Phase 5 — retire `/backtest` + housekeeping (small PR)

- Redirect `/backtest` → `/analysis`.
- Remove nav link.
- Update `CLAUDE.md`: the canonical doc for the V4 lens model.
- Migrate any docs referring to `/backtest` as a destination.

## Migration / backwards compat

- URLs without `?s=` continue to work — pages fall back to live data exactly as today. No breaking change for existing users / bookmarks.
- The live `trades` table keeps being written by the live position tracker; lens-off `/analysis` remains correct.
- `/backtest` keeps responding (as a redirect) for one release cycle, then can be deleted in V5.
- The existing `Chart.tsx` markers + `useTrades` connector pass-through stays functional for lens-off `/trading` (live markers render as before).

## Testing

Backend (target +12 tests, total ~118):

- `test_backtest_cache_hit` — same fingerprint, second call returns cached result without re-running engine.
- `test_backtest_cache_invalidation_on_params_change` — different params miss the cache.
- `test_insights_inline_trades_skips_db` — payload with inline `trades` does not query `trades` table (mock and assert no call).
- `test_insights_inline_payload_json_escapes_injection` — same prompt-injection guard the live path has.
- `test_signals_route_pagination_filters` — strategy + since cursor.
- `test_alerts_stats_aggregates_by_channel` — three rows, mixed success, returns correct counts.
- `test_test_webhook_route_discord_disabled` — when env var missing, returns 503 with reason.
- `test_test_webhook_route_fires_inapp_synthetic_signal` — observe inapp queue.

Frontend:

- tsc + visual: `/trading?s=trade_strat_v1&start=2026-04-22&end=2026-04-29` shows rail with control populated and chart with backtest markers.
- `/analysis?s=trade_strat_v1&...` shows lensed KPI + trades.
- Lens off: both pages show live data unchanged.
- Click row in 即時訊號 scrolls chart.
- Click 測試發送 next to a configured channel produces a toast + an entry in 通知遞送 list.

E2E: `docker compose up -d` + walk both pages with and without the lens, confirm the rail panels react.

## Risks

- **Backtest perf with long windows**. A 6-month window for `trade_strat_v1` loads >40k 5m bars + indicators. Phase 1's LRU and Phase 2's "fit chart window" default mitigate by encouraging short ranges. Cap: hard limit at 1 year, reject longer with `400`.
- **Cache invalidation on strategy code edit**. The hot-reloaded backend can serve stale results from the LRU after code changes. Include a version stamp (module mtime or git sha) in the fingerprint, OR clear the LRU on import. Choose mtime — cheap and exact.
- **Marker visual ambiguity (live vs backtest)**. Solo-resolved by the dotted-vs-dashed + hollow-vs-filled treatment. Add a one-line legend in the chart's top-left when both are present.
- **Inline insight payload size**. A high-frequency strategy could produce 500+ trades on a long window. The Anthropic call already JSON-encodes; large payloads burn cache. Cap inline trades at 200, summarize the rest server-side.
- **Channel "configured" detection leaks env vars**. The status endpoint returns booleans only (`{discord: configured: true/false}`); never echo the actual webhook URL. Confirmed in test.

## Out of scope (V5)

- Real auth (multi-user).
- Server-side persistent backtest cache (Redis / table).
- Per-trade fees / slippage / position sizing.
- Multi-strategy comparison overlay on the chart.
- Programmatic backtest scheduling / sweeps.
- Webhook templating per-channel payload.
- Tick-level intraday TP/SL replay.

## Critical files (anticipated)

Backend:
- `backend/app/backtest/engine.py` — add LRU + fingerprint with strategy-module mtime stamp.
- `backend/app/api/routes/backtest.py` — add `GET /backtest/run`.
- `backend/app/api/routes/insights.py` — accept optional inline `trades` / `stats`.
- `backend/app/api/routes/signals.py` (new) — `GET /signals`.
- `backend/app/api/routes/alerts.py` — extend with `/stats`.
- `backend/app/api/routes/admin.py` (or `backfill.py` companion) — `POST /admin/test-webhook`.

Frontend:
- `frontend/lib/lens.ts` (new).
- `frontend/lib/queries.ts` — `useBacktest` becomes a query; add `useSignals`, `useAlertStats`, `useTestWebhook` mutation.
- `frontend/components/StrategyLensControl.tsx` (new).
- `frontend/components/Chart.tsx` — add backtest-marker layer + window-aware live-marker hiding; remove inline DailyConfidenceBadge mount.
- `frontend/components/AlertLog.tsx` — channel chips, test button, mount seed.
- `frontend/app/trading/page.tsx` — rail composition.
- `frontend/app/analysis/page.tsx` — lens-driven data sources.
- `frontend/app/backtest/page.tsx` — convert to redirect.
- `frontend/components/ShellHeader.tsx` — drop `/backtest` nav link.
- `frontend/lib/i18n.ts` — new keys for lens / channel chips.

## Reuse — do not duplicate

- `app.backtest.engine.run_backtest` / `pair_into_trades` / `compute_backtest_stats` — DO NOT rewrite; just wrap with cache.
- `app.api.routes.trades.compute_stats` — already reused via SimpleNamespace adapters.
- `frontend/components/StrategySelector.tsx` — combobox shell can be lifted into the rail.
- `frontend/components/Skeleton.tsx`, `KpiCard.tsx`, `TradesTable.tsx` — feed them the lensed data, do not fork.
- `useStrategyState` — keeps powering `DailyConfidenceBadge` in its new rail location.
