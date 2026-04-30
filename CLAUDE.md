# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### One-command dev stack (recommended)

```sh
docker compose up           # db + backend + frontend, hot-reload all three
docker compose up --build   # first run, or after dependency changes
docker compose down         # clean stop (data preserved)
docker compose down -v      # also nuke ticks/signals/alerts/trades
```

Browser → `http://localhost:3000`. Backend on `:8000`. DB on `:5432`. Source is bind-mounted; both servers hot-reload on file edits.

**When `frontend/package.json` or `backend/pyproject.toml` changes**, plain `up` is not enough. The `/app/node_modules` (frontend) and `/app/.venv` (backend) mounts are anonymous volumes that Compose v2 preserves across `down`/`up` by stable hash, so a stale volume from a pre-dep-change build will still mask the new image's deps. Run the matching one-liner:

```sh
# frontend
docker compose stop frontend && docker rm -v taiex-frontend && docker compose up -d --build frontend

# backend
docker compose stop backend && docker rm -v taiex-backend && docker compose up -d --build backend
```

The `-v` flag removes the anon volume; `--build` regenerates the image with the new deps; `up` re-initializes a fresh anon volume from the new image.

### Host workflow (no Docker)

Use this when you want pytest, ruff, or alembic revisions outside containers.

#### Local infrastructure

```sh
docker compose up -d db          # TimescaleDB on 127.0.0.1:5432, volume taiex-pg
docker compose ps                # wait for STATUS=healthy
docker compose stop              # keep data
docker compose down -v           # nuke ticks/signals/alerts/trades
```

#### Backend (uv-managed; venv lives in `backend/.venv`)

```sh
cd backend
uv sync --extra dev                           # install runtime + dev deps
uv run alembic upgrade head                   # apply schema + continuous aggregates
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

uv run pytest -q                              # all tests
uv run pytest tests/test_indicators.py        # one file
uv run pytest -k "macd"                       # match by name
uv run pytest tests/test_notifier_hub.py::test_one_failure_does_not_block_others
uv run ruff check .                           # lint (config in pyproject.toml)

uv run alembic revision -m "msg" --autogenerate   # author a new migration
```

A new alembic migration **must** load `target_metadata = Base.metadata` from `app.db.models` (already wired in `app/db/migrations/env.py`). Continuous aggregates and Timescale extension setup are raw SQL inside the migration body — autogenerate cannot produce them; hand-edit.

#### Frontend

```sh
cd frontend
npm install
npm run dev                                   # binds 127.0.0.1:3000
npx tsc --noEmit                              # typecheck only
npm run build                                 # production build
```

### Sharing access (Tailscale)

```sh
tailscale up
tailscale serve --bg --https=443 http://127.0.0.1:3000
tailscale serve status                         # prints public-on-tailnet URL
tailscale serve --https=443 off                # stop
```

Both servers bind 127.0.0.1 by design. Tailscale Serve exposes only the Next dev port; `next.config.mjs` rewrites `/api/*` and `/ws/*` to FastAPI on `127.0.0.1:8000`, so a single proxy entry covers REST + WebSocket.

## Architecture

### Data flow (one process, asyncio)

```
FinMind 5-sec TAIEX
       │
       ▼
MarketDataAdapter (adapters/finmind_taiex.py)   ← swap point for shioaji feed
       │  Tick(ts, symbol, price, source)
       ▼
IngestRunner (ingest/runner.py)
   ├─► UPSERT into ticks hypertable (ON CONFLICT DO NOTHING)
   └─► fan-out per resolution: bar_update + bar_close
              │
              ├─► WebSocket /ws/stream (api/ws.py)
              │
              └─► StrategyLoop (runner/strategy_loop.py)
                       │  on bar_close: load bars, compute indicators,
                       │                call Strategy.on_bar()
                       ▼
                   Signal persisted → NotifierHub
                                          │ concurrent gather()
                                          ├─► DiscordNotifier
                                          ├─► N8nNotifier
                                          └─► InAppNotifier ─► WS + PositionTracker
                       (every attempt logged to alerts table)

PositionTracker (runner/position_tracker.py, V2)
   subscribes to InAppNotifier queue (same fan-out as WS — one source of
   signal events, no parallel queue). Pairs LONG↔EXIT/SHORT into rows in
   the `trades` table with `pnl_points`. Idempotent on signal id.
   Rehydrates open positions from DB on startup.
```

The **ingest runner is the single source of bar events**. Strategies and the WebSocket both subscribe to its in-process `asyncio.Queue` fan-out via `IngestRunner.subscribe(resolution)`. There is no separate scheduler — bar timing is derived from tick `ts` rounded into resolution buckets in `ingest/runner.py:_bucket_start`.

The **InAppNotifier queue is the single source of signal events** (V2). Both the WebSocket endpoint and the position tracker subscribe to it. Adding a new consumer of signals → call `hub.inapp.subscribe()`; do NOT add a parallel queue. The hub threads `signal_id` through the inapp payload so consumers can attribute their derived rows back to a `signals.id`.

### TimescaleDB schema

`ticks` is the only hypertable that holds raw data. Bars are derived:

- **1m, 5m, 15m, 30m, 1h, 4h, 12h, 1d** — Timescale **continuous aggregates** with a `add_continuous_aggregate_policy` running every 30 s.
- **1w, 1mo** — plain views built on top of `bars_1d` (continuous-aggregate-of-continuous-aggregate has restrictions, so the long buckets fall back to views).

The `/bars` endpoint in `app/api/routes/bars.py` reads whichever view matches the requested resolution. **There is no `bars` table.** Adding a new resolution means: add to `RESOLUTIONS` in `ingest/runner.py`, add to `VALID_RES` in `api/routes/bars.py`, and add the matching view to migration `0001_init`.

V2 added a plain Postgres `trades` table (migration `0002_trades.py`), populated by the position tracker. It is **not** a hypertable (low row volume) and has a partial unique index `ux_trades_open_position` on `(strategy, symbol) WHERE exit_ts IS NULL` so a double-open race between the strategy loop and a tracker restart cannot land two open rows for the same pair. Closed rows are unconstrained. FKs to `signals.id` use `ON DELETE SET NULL` so signal purges don't cascade-delete trade history.

### Indicator service

Five hand-rolled indicators in `app/indicators/`: MA, MACD, RSI (Wilder), KD (TW-style 3-EMA smoothing), DMI (Wilder ADX). **No `pandas-ta` dependency** — the unmaintained package breaks on numpy 2 / pandas 3. `IndicatorCache` in `service.py` keys on `(symbol, resolution, kind, frozen-params)` and invalidates only when the latest bar timestamp moves, so the same series is reused across REST + strategy evaluations within one bar.

### Strategy plug-in framework

`Strategy` ABC + `BarEvent`/`Signal` dataclasses live in `app/strategies/base.py`. The registry in `app/strategies/registry.py` does two things:

1. **In-repo:** `discover()` walks `app/strategies/examples/` and imports every module, picking up anything decorated with `@register_strategy`.
2. **External:** also loads any `taiex.strategies` Python entry-point group, so a strategy can ship as a separately-installed pip package.

A strategy declares `resolutions: list[str]` and an optional `indicator_specs: dict[label, {kind, params}]`; the runner precomputes those indicators and passes them in via `BarEvent.indicators`. Per-strategy `enabled`, `params`, and `channels` live in the `strategy_config` table and are managed via `/strategies` REST endpoints.

V2 changed the downstream consequences of a `Signal`: alongside the notifier fan-out, the position tracker pairs LONG/SHORT/EXIT/FLAT into rows in the `trades` table. Same-direction signals are no-ops (no stacking); opposite-direction signals atomically close the open trade and open a fresh one at the same price/timestamp; same-id replays are idempotent. **Strategies that only emit `LONG` (like the V1 `always_long` example) never close a trade, so they never contribute to win-rate or PnL** — pair entries with an exit rule unless you specifically want a watchdog signal. The full pairing truth table and a worked LONG↔SHORT example live in `NOTES.md` §7.

### Historical backfill (V2.5)

`app/ingest/backfill.py` fills the gap between the live `taiwan_futures_snapshot` (real-time only, no history) and the user's expectation that closing the laptop should not lose data. It hits FinMind's historical `TaiwanFuturesTick` dataset (Backer/Sponsor tier) per market day, inserts ticks via the same `ON CONFLICT DO NOTHING` path the live ingest uses (with `source = "FINMIND_FUTURES_TICK"` so the two streams stay distinguishable). Inserts are chunked at 5000 rows/query because Postgres caps a single statement at 65,535 bind parameters and a full day of `MTX` ticks is 200k+ rows.

Two entry points: `BackfillService.backfill_range(start, end)` (manual, exposed at `POST /admin/backfill?start=&end=`) and `BackfillService.backfill_recent(lookback_days)` (auto-fired in lifespan, scans the last N days for under-filled market days and refetches them — `BACKFILL_ON_STARTUP_DAYS=0` to disable).

Today is always skipped during auto-backfill — TaiwanFuturesTick updates end-of-day, so the current session is never complete in the dataset until tonight. The live ingest fills the rest.

### Data quality fixes (V2.6)

Three pollution paths produced wicks-to-zero / wicks-to-baseline on the chart. All three are now fixed at the ingest boundary; a single shared `app.ingest.constants.PRICE_FLOOR = 1000.0` is the canonical sanity floor.

1. **Zero-priced live ticks.** FinMind `taiwan_futures_snapshot` occasionally returns `close: 0` between trades. The continuous aggregate `min(price)` collapsed to 0 → wicks-to-zero on every bar. `app.adapters.finmind_taiex._rows_to_ticks` now drops rows with `price < PRICE_FLOOR`.

2. **Calendar-spread quotes in the historical backfill.** FinMind `TaiwanFuturesTick` mixes outright single-leg trades (`contract_date='202605'`, price ~39k) with TAIFEX-listed combo orders (`contract_date='202604W5/202605'`, price ~86–700 representing the spread differential). `app.ingest.backfill.FinmindHistoricalClient.fetch_day` rejects rows where `'/' in contract_date` plus the same sub-floor guard.

3. **Multi-contract pollution.** The snapshot endpoint returns ALL contract expiries for a product (TXFE6 front, TXFR1 rolling-front alias, TXFR2 next-week, TXFG6/I6/L6 back-months, TXFC7 far-month at +1000pt carry premium). Inserting all of them as the same symbol made the chart bounce between contracts. `_pick_front_month` now picks one row per poll: prefer `futures_id` ending `R1` (TAIFEX continuous-rolling alias), else highest `total_volume`, tie-break by smallest numeric `contract_date`. The historical backfill applies the analogous filter — count `contract_date` values, keep only the most-traded one per day. **Rows with empty `contract_date` get a free pass** (legacy payload shape compatibility); the filter logs a warning when it can't discriminate.

### TAIFEX after-hours session (V2.6)

The TAIFEX 夜盤 runs `15:00` Taipei through `05:00` the next morning (Mon-Fri evening start; Sat 00:00–05:00 belongs to Friday's session; Sat after 05:00 and all of Sunday are closed). The adapter previously only allowed the regular session `08:45–13:45` and went silent after 13:45 until next morning, which made live ingest dead for two-thirds of TAIFEX trading hours.

Two new settings in `app/config.py`: `night_session_open=15:00`, `night_session_close=05:00`. `FinMindTaiexAdapter._market_open` evaluates day session OR night-evening (Mon-Fri ≥15:00) OR night-overnight (Tue-Sat ≤05:00). `_next_open` walks forward minute-by-minute (bounded at 4 days) — slow but only invoked when the market is closed.

### Recovery scripts (V2.6)

Two one-shot scripts under `backend/scripts/`:

- `purge_zero_ticks.py` — deletes `WHERE price < PRICE_FLOOR` and refreshes all 8 continuous aggregates. Use after ingest fixes when the existing `ticks` rows still contain a small known-bad subset (zero ticks, sub-floor noise) but the bulk is good.

- `wipe_and_rebackfill.py` — `TRUNCATE ticks` + cagg refresh + relies on the next backend restart to re-run auto-backfill with the current ingest logic. Use when the existing data is heterogeneously polluted (e.g. mixed contract expiries) and selective deletion can't help. Destructive — has no env guard, run inside the dev container only.

Both scripts use a separate `AUTOCOMMIT` connection for the `CALL refresh_continuous_aggregate` calls because TimescaleDB rejects them inside a transaction.

### V2 REST routes (additions only)

- `GET /trades?strategy=&start=&end=&result=win|loss|all&limit=` — list. Date-only `end` strings are interpreted as start-of-next-day exclusive (so a `today` filter does not silently drop intraday trades).
- `GET /trades/stats?strategy=&start=&end=` — aggregate (`trade_count`, `open_count`, `win_count`, `loss_count`, `win_rate`, `pnl_total`, `pnl_avg_win`, `pnl_avg_loss`, `max_drawdown`, `avg_hold_seconds`). Drawdown is reported as a positive magnitude (peak − cum); UI negates for display.
- `GET /status` — `{ ok, ingest_running, last_tick_ts, ingest_lag_seconds, strategy_loop_running, position_tracker_running, db_ok, notifiers: { discord, n8n, inapp } }`. Powers the status pill.
- `POST /insights/strategy` — body `{ strategy, start, end, filter }`, returns `{ cached, generated_at, content }`. 503 when `ANTHROPIC_API_KEY` is unset; 429 when rate-limited (with `Retry-After` header).
- `POST /admin/backfill?start=YYYY-MM-DD&end=YYYY-MM-DD` — historical tick backfill from FinMind. Returns `{ start, end, days: [{day, fetched, inserted, error}], total_inserted, total_fetched }`. 503 when `FINMIND_TOKEN` is unset.

### Notifier hub

`NotifierHub.dispatch()` runs all configured notifiers in `asyncio.gather` and writes one `alerts` row per channel attempt. Failures are caught per-notifier; one bad webhook never starves the others. The `InAppNotifier` is in-process and publishes onto a queue that the WebSocket endpoint subscribes to — operators see signals in the dashboard even when both webhooks are misconfigured.

### Configuration

`app/config.py` uses `pydantic-settings` with `env_file=("../.env", ".env")`, so the same `.env` at repo root works whether commands run from `backend/` or the project root. Settings are cached via `@lru_cache` on `get_settings`. The display symbol (`SYMBOL_DISPLAY`, default `MXF`) is decoupled from the source (`SYMBOL_SOURCE`, default `TXF`) — the adapter labels every tick with `symbol_display`, so the chart can read "MXF" while the data comes from TXF. **Important:** the FinMind sponsor `taiwan_futures_snapshot` endpoint serves `TXF / TMF / CDF` only; `data_id=MXF` returns zero rows and silently freezes the feed. We briefly tried `SYMBOL_SOURCE=MXF` early in V2 — it broke the live feed mid-session — and reverted. TXF and MXF both track the same TAIEX index, so labelling TXF data as MXF in the UI is semantically fine.

V2 added optional Anthropic settings (`anthropic_api_key: SecretStr | None`, `anthropic_model: str = "claude-sonnet-4-6"`, `insights_cache_ttl_seconds`, `insights_cache_max_entries`). When `anthropic_api_key` is unset, `POST /insights/strategy` returns 503 and the frontend AI panel degrades cleanly — the rest of the app works.

V2.6 added `night_session_open` (default `15:00`) and `night_session_close` (default `05:00`) for TAIFEX after-hours coverage — see "TAIFEX after-hours session" above. Override per-deployment via `NIGHT_SESSION_OPEN` / `NIGHT_SESSION_CLOSE` env vars if your broker / data provider has different boundaries.

### AI insights service (V2)

`app/services/insights.py` calls Sonnet 4.6 via the Anthropic SDK with prompt caching (`cache_control: ephemeral` on the system prompt). The user message JSON-encodes the trade payload — never f-string interpolated — so a malicious `Signal.payload.reason` from a future strategy cannot break out of JSON and inject instructions. The system prompt also explicitly tells the model to treat trade-row data as non-executable. Tests assert this escape behaviour. **When editing the system prompt, ensure the `cache_control` marker stays on the *last* system content block and the prompt remains a true module-level constant** — any byte change (including a stray `datetime.now()` interpolation) silently invalidates the prefix cache. The minimum cacheable prefix on Sonnet 4.6 is 2048 tokens; the current prompt is shorter, so caching is wired but currently a no-op in practice.

`app/services/insights_cache.py` is an in-process bounded TTL+LRU on `OrderedDict`, monotonic-time for DST safety. Key fingerprint hashes sorted `(trade_id, pnl_points)` tuples plus the filter so two distinct distributions with the same count and total PnL still get distinct cache slots. Restart drops the cache — intentional, no Redis dependency.

`POST /insights/strategy` enforces a 5/min/(strategy, ip) token bucket inside an LRU dict capped at 1024 keys (so a unique-IP spray cannot grow it unboundedly). Honours `X-Forwarded-For`. Behind a reverse proxy that strips it, the limit collapses to a single bucket — flagged in V3.

### Frontend

Next.js 15 App Router, single locale (`zh-Hant-TW`). `lib/i18n.ts` is a tiny dict + `t()` helper. **Indicator names stay English** by design — never wrapped in `t()`.

V2 split the dashboard into two routes under one shared layout (`app/layout.tsx` → `ShellHeader.tsx` with brand + nav + status pill):

- **`/trading`** — `app/trading/page.tsx`. TopBar (resolution + StrategySelector combobox + IndicatorToggleBar) + Chart + AlertLog right rail.
- **`/analysis`** — `app/analysis/page.tsx`. KPI strip + TradeFilterBar + TradesTable + TradeInsightPanel (deterministic 模式分析 + manual `生成洞察` AI button).
- `app/page.tsx` is a server-side `redirect("/trading")`.

Active strategy is propagated across pages via the URL query param `?s=<name>` (read with `useSearchParams`); both pages stay in sync without context plumbing. Anything that reads `useSearchParams` must be wrapped in `<Suspense>` for Next 15's prerender pass.

The chart (`components/Chart.tsx`) uses **TradingView Lightweight Charts v5**. V2 refactored from `priceScaleId` stacking to true panes via `chart.addPane()` + `chart.addSeries(..., paneIndex)`; MACD / RSI / KD / DMI each get their own pane (MA stays on the price pane because that's where moving averages belong). **Candle convention is TW: red = up 漲 (#c0392b), green = down 跌 (#3a7d4f)** — opposite of the US convention. Histograms and DMI lines follow the same colour grammar.

`ChartCrosshairTooltip.tsx` is a separate React overlay subscribed to `chart.subscribeCrosshairMove`. It reads from a `Map<time, values>` lookup populated alongside `series.setData` and patched on bar updates — **no network call on hover**. When toggling indicators on/off, that map must be cleared/rebuilt for the affected series.

Time on the chart axis and built-in tooltip is rendered through `Intl.DateTimeFormat({ timeZone: "Asia/Taipei", locale: "zh-Hant-TW" })` via `localization.timeFormatter` and `timeScale.tickMarkFormatter`. lightweight-charts has no native timezone option — these formatters are the only way to show CST on the axis. **Don't pass UTC-shifted epoch seconds to the chart** as a workaround; it breaks crosshair lookups, since the lookup map keys are the original (UTC) `time` values.

Live updates merge into the chart in `Chart.tsx`: every `bar_update` WS message either appends a new bar or extends the in-progress one (high/low/close mutate, open is sticky). Bar history is fetched once via `/api/bars` on resolution change; ongoing bars come from the WS only. The InAppNotifier signal payload now carries `id` (the `signals.id`) so the position tracker can attribute and the WS consumers can deduplicate.

`lib/ws.ts` derives the WebSocket URL from `window.location` (not from an env var) so the same code works on `127.0.0.1:3000` and on a Tailscale Serve hostname without configuration. The `next.config.mjs` rewrite for `/ws/:path*` is what makes that work.

`lib/queries.ts` (V2) is the single home for TanStack Query hooks: `useStatus`, `useTrades`, `useTradeStats`, `useInsight` (mutation, manual trigger). Components import from there rather than calling `fetch` directly.

V2.6 polish pass: typography scale tokens (`--fs-caption/meta/body/subhead/head/num-lg`) replace hardcoded `font-size` literals across `globals.css` and inline styles; base body lifts 14→15.5 px. Elevation tokens (`--shadow-sm/md/card/pop`) on header, KPI cards, popover, combobox listbox, insight panel. Focus rings on every interactive element via `:focus-visible`. Hover-lift micro-interactions on buttons and pills (with `prefers-reduced-motion` honored). New `Skeleton.tsx` component with shimmer; `KpiCard`, `TradesTable` (`aria-busy` + sr-only caption), and `TradeInsightPanel` accept an `isLoading` prop and render skeletons during initial fetch. Lucide-react icons replace unicode glyphs (`Settings` in `StrategySelector`, `RefreshCw` in `TopBar`).

The TopBar now hosts a manual refresh button (lucide `RefreshCw`, 44×44 touch target, `aria-label="重新整理 K 線"`). Click invalidates `["bars", res]` and `["indicators"]` TanStack Query keys via `useQueryClient`; the button spins (`.spinning` class on `@keyframes spin` from `globals.css`) until both refetches resolve. **Adding a new npm dep requires a backend rebuild flow** — see "When `frontend/package.json` or `backend/pyproject.toml` changes" in the dev-stack section above; plain `docker compose up` reuses a stale anonymous `node_modules` volume and won't pick up new packages.

### Tests

`backend/tests/` covers indicator math (against straight-uptrend fixtures), notifier hub fan-out + per-channel failure isolation + channel filter, FinMind adapter dedupe + invalid-row tolerance + sub-floor rejection + front-month picker (R1 alias preference, volume-fallback, contract_date tiebreak, NaN-safe coercion) + day/night `_market_open` boundary cases, V2 position tracker (open/close/flip/idempotency/rehydrate), V2 trades API (`compute_stats` win rate / drawdown / avg hold extracted as a pure function specifically so tests can call it without DB), V2 insights cache (TTL + LRU + key sensitivity), V2 insights service (system-prompt persona, `cache_control` marker, JSON-encoded payload escapes a known prompt-injection string), V2.5 backfill (trading-day filter, FinMind client parse + quota path, `_missing_days` threshold + today-skip, range/recent flows), and V2.6 backfill spread/floor/dominant-contract filters. **76 tests as of V2.6.**

**None of the tests require a live database.** When adding a feature that needs DB I/O, mock at the `session_scope()` boundary or extract the SQL-touching logic into a pure function the test can patch directly (this is what `compute_stats` does — the SQL part is in `_query_trades`, the math is separate).

### V3 — Candle merge fix + UI polish + strategy plotting + backtest engine

#### Candle in-progress bar fix

`/bars` (`app/api/routes/bars.py`) now appends `bucket < :cutoff` where `cutoff = _bucket_start(now_utc, resolution)` from `app/ingest/runner.py`. The endpoint returns ONLY closed historical buckets — the WebSocket stream is the sole source of the live in-progress bar. This eliminates the visible "30s reset" of the live candle that the continuous-aggregate-refresh-policy lag was causing.

`Chart.tsx` keeps `lastBarRef` as authoritative for the in-progress bucket. The bars-effect re-overlays `lastBarRef` after `setData(history)` if its `time` is strictly newer than the last historical bar. A `prevResRef` clears `lastBarRef` on resolution change so a stale 1m bar cannot leak into a 5m series. `refetchInterval` on `useQuery(['bars',res])` is `300_000` (5min) — refetch is now a recovery mechanism, not the primary update path.

`IngestRunner` got a watchdog (`_watchdog_loop` + `_watchdog_tick`) that fires every 5s and force-closes any `_open_buckets[res]` older than `3 × RESOLUTION_DELTAS[res]` (3-bar grace covers FinMind's typical reconnect latency). A tombstone set `_closed_buckets` (bounded to 4 entries per resolution) blocks delayed ticks from re-seeding a force-closed bucket, preventing double `bar_close` emits.

#### UI polish (ui-ux-pro-max-guided)

Token additions in `globals.css` (additive only): `--fs-section: 22px`, `--fs-display: 32px`, `--fs-num-xl: 36px`, `--fw-semi: 600`, `--fw-bold: 700`, spacing scale `--space-1..7`, easing tokens `--ease-out/in/spring`, motion tokens `--dur-fast/base/slow`. Bumped `--fs-body` 15.5→16 px, `--fs-subhead` 16→17 px.

Keyframes added (transform/opacity only, with explicit `prefers-reduced-motion: reduce` overrides):
- `fadeInUp` (`opacity 0→1, translateY(8px)→0`).
- `underlineGrow` (`scaleX(0)→1, transform-origin: left`).
- `pulseAccent` (one-shot `box-shadow` ring expansion, no infinite loop).

`.section-title` (Noto Serif TC, `--fs-section`, `--fw-bold`, tight tracking, accent rule via `::after` + `underlineGrow`) is the canonical heading style. Applied to all panel headings (AlertLog, TradeInsightPanel, equity panel, trades panel). KPI card stagger via `:nth-child` `animation-delay: 0/40/80/120ms`. `font-variant-numeric: tabular-nums` on every price/PnL/timestamp column. TopBar refresh button toggles a `.pulse-success` class on completion. Layout grid `1fr 340px` and TW candle palette unchanged.

#### `trade_strat_v1` — multi-timeframe strategy

`app/strategies/examples/trade_strat_v1.py` declares `resolutions = ["5m", "30m", "1d"]`.

- **Entry** (30m): `KD>20` AND `MACD>0` AND `+DI>21`, fires only on rising edge (conditions just turned true).
- **Exit assist** (5m, substituted from spec's 3m since `RESOLUTIONS` doesn't include 3m): `-DI>23` while LONG → emit EXIT.
- **TP/SL** (30m): `+220 / -60 pt` checked on bar close.
- **Daily confidence** (1d): tracks 0..3 long-side and 0..3 short-side condition counts. Display only — never blocks entry.
- **Discipline**: 1 contract no pyramiding, 5×30m-bar cooldown after exit, freshness filter on rising edge.
- **Fill convention deviation**: signals fire on bar close (framework limitation). Spec calls for next-bar-open fill — documented in module docstring as deferred.

Strategy is recreated per `bar_close`, so position / cooldown state lives in module-level `_STATE: dict[(name, symbol), _StratState]`. The base `Strategy` ABC now has an optional `dump_state(symbol) -> dict` classmethod (default `{}`); `trade_strat_v1` implements it. `GET /strategies/{name}/state` exposes the snapshot via `app/api/routes/strategies.py`.

#### Chart strategy plot overlays

`Chart.tsx` consumes WS `signal` messages and the `useTrades` hook to render three overlay layers on the price pane:

- **Markers** via lightweight-charts v5 `createSeriesMarkers(series, [])` — entry arrows (red `arrowUp` LONG below bar / green `arrowDown` SHORT above bar) and exit circles (red TP / green SL / accent DI flip). Idempotent on `signal.id` via `seenSignalIdsRef`.
- **Entry / TP / SL price lines** via `series.createPriceLine(...)` — drawn on entry, torn down on exit. Line styles: entry grey dashed, TP red dotted, SL green dotted.
- **Entry→exit dashed connector** — single `LineSeries` (dashed grey, `lineWidth: 1`). Segments built from closed `useTrades({strategy, result: "all"})` rows, separated by whitespace data points (`{time}` no `value`) so unrelated trades don't visually link.

`DailyConfidenceBadge.tsx` is a top-right chart overlay for `trade_strat_v1`: 多/空 0..3 dot rows + position summary line. Hidden on cold start. Polls `useStrategyState(name)` every 60s.

#### Backtest engine — Pine-Script Strategy Tester

`app/backtest/engine.py` replays a registered strategy across closed historical bars and produces a Pine-Script-style result. `POST /backtest/run` accepts `{strategy, symbol?, start, end, params?}` and returns `{strategy, symbol, start, end, params, resolutions, bar_counts, signals[], trades[], stats, equity_curve[]}`.

Engine details:
- `load_bars` per declared resolution (reuses the same `/bars` cutoff that excludes the in-progress bucket — perfect for backtest).
- Indicators precomputed via `indicator_cache.get` (warm across param sweeps).
- Schedule interleaves bar_close events from all resolutions chronologically with smaller-resolution-first tie-break (so 5m fires before its containing 30m on a shared boundary).
- `_swap_state` / `_restore_state` snapshot the strategy module's `_STATE[(name, symbol)]` before the run and restore after, so backtests cannot pollute live in-process state. Convention: any module-level `_STATE: dict` keyed by `(strategy_name, symbol)` is detected automatically; stateless strategies pay nothing.
- `pair_into_trades` is a pure function mirroring `PositionTracker` (LONG/SHORT/EXIT/FLAT, reverse-on-opposite, no-op same-direction).
- `compute_backtest_stats` reuses `app.api.routes.trades.compute_stats` via `SimpleNamespace` adapters and adds Pine-Script extras: `profit_factor`, `largest_win`, `largest_loss`, `avg_bars_in_trade`.

Frontend `/backtest` page was a standalone route in V3 (form → KPI strip → equity curve → trades table). **Retired in V4** — `app/backtest/page.tsx` is now a server-side `redirect("/analysis?compare=1&s=trade_strat_v1&s2=trade_strat_v2")` for old bookmarks; the same KPI / equity / trades surface is now lens-driven inside `/analysis` (compare mode renders side-by-side). The backtest *engine* and `/backtest/run` REST route are unchanged.

### Tests

`backend/tests/` covers everything from V2.6 plus V3 additions: `/bars` cutoff exclusion, `IngestRunner` watchdog tick + tombstone double-emit guard + grace window, `trade_strat_v1` (dump_state shape, daily confidence count, rising-edge entry, no-repeat-without-reset), `/strategies/{name}/state` route (404 unknown / `{}` stateless / populated stateful), backtest engine (pair logic for long+exit / reverse / same-direction / orphan exit, stats math, equity curve cumulative, end-to-end smoke with stub strategy + patched `load_bars`, state isolation, empty-history + 404). **106 tests as of V3.**

**None of the tests require a live database.** When adding a feature that needs DB I/O, mock at the `session_scope()` boundary or extract the SQL-touching logic into a pure function the test can patch directly (this is what `compute_stats` does — the SQL part is in `_query_trades`, the math is separate).

### Backlog and security

V3 shipped: candle in-progress bar fix, watchdog force-close, ui-ux-pro-max polish, `trade_strat_v1`, chart plot overlays (markers / price lines / connector), backtest engine + `/backtest/run`. Items still deferred from V2 / V2.5 (do NOT silently re-introduce as new ideas — check `V3_plan.md`): CORS still wide open, mutating endpoints unauthenticated (`/strategies/*`, `/insights/strategy`, `/admin/backfill`, `/backtest/run`), no global Anthropic spend cap, reverse-proxy IP gap on the rate limiter, no TW holiday calendar (V2.5 backfill iterates Mon-Fri including holidays — wasted API calls), `/admin/backfill` synchronous (multi-month windows block — to make streaming/background), no per-trade fees/slippage, no auth/multi-user.

V2.6 known issues still open: `wipe_and_rebackfill.py` is a destructive script with no environment guard (gate behind env-name assertion), the front-month picker assumes FinMind keeps using the `R1` rolling-alias suffix (log when fallback path triggers, warn if `R1` rows ever disappear from the response), and the dominant-contract backfill filter falls through to "keep everything" when all rows have empty `contract_date` (logs a warning but doesn't fail — decide whether to abort or accept).

V3 introduced new known-issue items, addressed in `v4_plan.md`:
- `/backtest` is a top-level route. V4 retires it; strategy + window become a global lens that drives `/trading` and `/analysis`. Old bookmarks to `/backtest` should redirect.
- 即時訊號 / 通知遞送 panels exist visually but the underlying wiring (Discord / n8n configured-state surfacing, signal seeding on mount, persistent test-webhook affordance) is partial. V4 phase 4 makes them first-class.
- `POST /backtest/run` has no result cache — repeat calls re-run the engine. V4 phase 1 adds an LRU keyed on `(strategy, params, symbol, start, end, module_mtime)`.
- Backtest engine fills at signal-bar close (no `next_bar_open` mode) and has no commission / slippage / position sizing. V5+.
- `_STATE` swap convention is module-introspection-based. Reasonable for v1 stateful strategies but brittle if a strategy uses a non-`_STATE` name. Document the convention in any new strategy template.

**`v4_plan.md` is the canonical scope reference for the next session.** It defines the strategy-as-lens model, the right-rail composition on `/trading`, the lens-driven `/analysis`, working alert plumbing (`/alerts/stats`, `/admin/test-webhook`, channel chips), and a 5-phase rollout. Read it before starting V4 work.

### V4 — Strategy as global lens + alert plumbing + chart polish

#### Strategy + window as a global lens

`frontend/lib/lens.ts` is the canonical state hook. URL params `?s=&s2=&start=&end=&res=&ind=&compare=` are the source of truth; `localStorage` key `taiex.lens.v1` mirrors them and seeds on cold mount when the URL has no params. `s` is the primary strategy, `s2` is the comparison strategy (compare mode), `start`/`end` are ISO dates, `res` is the chart resolution, `ind` is a comma-joined indicator allow-list, `compare=1` flips compare mode on. Both `/trading` and `/analysis` consume the same hook; resolution + indicator selections persist across navigation. `ShellHeader` nav links forward the current querystring (existing behaviour, retained).

#### Backtest engine LRU

`app.backtest.engine` adds an in-process LRU keyed on `(strategy, params_hash, symbol, start, end, module_mtime)`. Both `POST /backtest/run` (existing) and the new `GET /backtest/run` (idempotent, query-param form for the lens `useBacktest` hook) share the cache. `module_mtime` of the strategy module file invalidates the cache after a strategy code edit so a hot-reloaded strategy never serves stale results.

#### `trade_strat_v2`

Clone of `trade_strat_v1` with the entry / TP / SL timeframe shifted from 30m → 10m and the exit-assist timeframe shifted from 5m → 2m; daily confidence on 1d unchanged. Declares `resolutions = ["2m", "10m", "1d"]`. New alembic migration `0003_bars_2m_10m.py` adds the `bars_2m` and `bars_10m` continuous aggregates with the same 30 s `add_continuous_aggregate_policy` as the V1 buckets. `RESOLUTIONS` in `ingest/runner.py` and `VALID_RES` in `api/routes/bars.py` updated accordingly.

#### Chart polish

Cursor-price line in `ChartCrosshairTooltip.tsx` via `series.coordinateToPrice` (shows the price at the crosshair Y, not just the bar OHLC). Top-left `HiLoBadge.tsx` overlay subscribes to `chart.timeScale().subscribeVisibleLogicalRangeChange` and reports the high/low of the *visible* range — recomputes only when the visible range moves. CSS strategy color tokens `--strategy-1` / `--strategy-2` drive marker / connector / equity-line colours so primary vs comparison strategies are visually disambiguated everywhere. Live + backtest marker layers coexist on the chart: live markers are filled, backtest markers are half-opacity dotted to keep them legible without competing for attention. `DailyConfidenceBadge` moved out of the chart overlay into the right rail on `/trading` so it doesn't overlap with the new `HiLoBadge` corner real estate.

#### `/analysis` lens-driven KPI / trades / insight

When the lens has a window selected, `useBacktest()` feeds the KPI strip and trades table — `/analysis` now shows backtest results for the same `(strategy, start, end)` lens that `/trading` is plotting. With `compare=1`, two `useBacktest` calls run in parallel and the page renders side-by-side `1fr 1fr` columns (left primary, right comparison). The AI insight panel posts trades + stats inline; comparison mode posts `compare_a` / `compare_b` payloads, and the backend appends a second `cache_control: ephemeral` system block (`COMPARE_SYSTEM_TAIL`) — the original `SYSTEM_PROMPT` constant stays byte-unchanged so the live-mode prefix cache survives.

#### Working alert plumbing

`GET /signals?strategy=&since=&limit=` seeds the 即時訊號 panel on mount (previously cold-started empty until a new signal fired). `GET /alerts/stats` aggregates per channel: `{ channel: {sent, failed, last_ts} }`, powering the channel chips. `POST /admin/test-webhook?channel=discord|n8n` fires a synthetic `Signal` through one notifier; returns 503 if the channel's env var is unset, 200 with `{ok}` on success. Right-rail 通知遞送 panel shows channel health chips with a 測試發送 button per channel. Click on a 即時訊號 row dispatches a `chart-scroll-to` `CustomEvent` that the chart on `/trading` listens for and pans the time scale to the bar.

#### `/backtest` retired

`frontend/app/backtest/page.tsx` is now a server-side `redirect("/analysis?compare=1&s=trade_strat_v1&s2=trade_strat_v2")` (Next 15 idiomatic). `ShellHeader.tsx` `NAV` array no longer includes the entry; the `"nav.backtest"` key remains in `lib/i18n.ts` (harmless, no callers). Old bookmarks resolve via the redirect. The backtest *engine* (`app/backtest/engine.py`) and the `POST /backtest/run` REST route are unchanged — only the standalone UI page is gone.

#### Tests

V4 added tests for: lens URL/localStorage round-trip, `GET /backtest/run` shape parity with `POST`, backtest LRU hit/miss with module_mtime invalidation, `trade_strat_v2` (entry/exit/TP/SL on the 10m/2m timeframes, dump_state shape), `bars_2m` + `bars_10m` route presence, `GET /signals` filter by `strategy`/`since`/`limit`, `GET /alerts/stats` aggregation, `POST /admin/test-webhook` 503-when-unset / 200-success / channel-routing, `/insights/strategy` compare mode (`compare_a`/`compare_b` payload, `COMPARE_SYSTEM_TAIL` tail block, original `SYSTEM_PROMPT` constant unchanged so prefix cache survives). **145 tests as of V4.** Confirm via `cd backend && uv run pytest -q | tail -1`.

#### Backlog still deferred

V4 did not address: CORS still wide open, mutating endpoints unauthenticated (`/strategies/*`, `/insights/strategy`, `/admin/backfill`, `/admin/test-webhook`, `/backtest/run`), no global Anthropic spend cap, reverse-proxy IP gap on the rate limiter, no TW holiday calendar, `/admin/backfill` synchronous, no per-trade fees / slippage / position sizing in the backtest engine, no auth / multi-user, backtest engine still fills at signal-bar close (no `next_bar_open` mode), `_STATE` swap convention is module-introspection-based and brittle to non-`_STATE` naming, `wipe_and_rebackfill.py` still has no env guard.

### V5 — strategy spec compliance + chart marker hardening + trades-log indicator columns

V5 was driven by five intertwined user asks: live exits violated spec (TP fires above 220), exit dots missing on chart, trades log needed sequential id + strategy column + open/close indicator deltas, strategy display labels needed Traditional Chinese rename without breaking the DB key, and the v1/v2 spec doc had drifted from the example strategies.

#### Backend

- alembic `0004_bars_3m.py` added the `bars_3m` continuous aggregate (mirrors `0003_bars_2m_10m.py`). `RESOLUTIONS`, `RESOLUTION_DELTAS`, `VALID_RES` extended.
- `Strategy` ABC gained `display_name: ClassVar[str | None] = None`. Surfaced via `StrategyOut.display_name`. The DB-bound `name` ClassVar stays the canonical key for `trades.strategy`, `signals.strategy`, `strategy_config.name`. UI renders `display_name ?? name`. Backend never accepts `display_name` as input on `/strategies/{name}/*` routes.
- `trade_strat_v1` rewrite to match `v1_trade.md`: 30m entry, 3m exit-assist (-DI > 23), MACD rising-edge gate (`macd[-3] <= 0 AND macd[-2] > 0 AND macd[-1] > macd[-2]`), `+DI > 21 AND +DI > -DI` for LONG, TP=220 / SL=60, `display_name = "30分鐘線策略"`.
- `trade_strat_v2` rewrite to match `v2_trade.md`: 5m entry (was 10m), 3m exit-assist with `-DI ≥ 23` (note `>=` not `>` — distinct from v1), 1m TP/SL eval as a separate `_check_tp_sl_minute` code path with no entry logic, TP=70 / SL=50, symmetric SHORT MACD falling-edge via `_macd_rising_edge(-macd)`, display_name = "5分鐘策略". `_snapshot_ind` uses fixed 8-key `dict.fromkeys(...)` shape so the frontend `TradeIndicators` type contract is stable.
- `Signal.payload` gains `entry_ind` (open) and `exit_ind` (close) as 8-key snapshots `{k, d, macd, signal, hist, plus_di, minus_di, adx}` rounded to 2 decimals, NaN → None. `PositionTracker._open_trade` writes `entry_ind` into `Trade.payload`; `_close` does an `UPDATE … SET payload = COALESCE(payload, '{}'::jsonb) || jsonb_build_object('exit_ind', :exit_ind)` second statement so the existing `entry_ind` is preserved.

#### Frontend

- `Chart.tsx` exit-dot fix: pane-relative y-clamp via `chart.paneSize(0).height` (defensive try/catch fallback to canvas height with a one-shot warning); when clamped, `paintMarker` paints a directional `▲` / `▼` chevron beside the disc so the user sees off-screen exits. Hover hit-test uses a `paintedCoordsRef: Map<TradeEvent, {x, y, outOfRange}>` populated during `drawOverlay`, so clamped markers are now hoverable. `window.__taiexMarkerStats` exposes `{events, drewOpen, drewClose, skipped, clamped, retryScheduled, hitTestable}` for browser-console debugging. The 250ms retry from commit `9da8078` is retained.
- New `MarkerFilterPills.tsx` in TopBar — 全部 / per-strategy display name. Local component state in `app/trading/page.tsx`; `<Chart>` accepts a new `markerStrategies?: Set<string> | null` prop (null = show all). Filters in a `filteredTradeEvents` memo downstream so paint + hover both inherit.
- `TradeMarkerTooltip` adds a `#${tradeId}` chip in the head row (sumi-gold `.trade-marker-id` class).
- `TradesTable.tsx` adds 4 columns — 編號 (`tr.id`), 策略 (display_name fallback to canonical), 開倉指標, 出場指標 — rendered via `formatIndicators` as `K54 D51 / MACD+9 / +DI33 -DI19`. Older trades pre-V5 with empty payload render `—`. Header copy is TC; indicator names (K, D, MACD, +DI, -DI) stay English. Strategy cell carries `title={tr.strategy}` so canonical name is on hover.
- `StrategySelector` and `AlertLog` render `display_name ?? name` with `title={canonical_name}` on hover. Search/filter still keys on `name` for cache stability.
- `lib/api.ts` adds `TradeIndicators` + `TradePayload` types; `Trade.payload` narrows from `Record<string, unknown>` to `TradePayload`.
- `lib/queries.ts` exposes a `useStrategies` hook (was inline in `StrategySelector`); cache key shared.

#### Tests

178 tests as of V5 baseline (was 145).

### V5.1 — pane-height persistence + trade_strat_v1 exit re-spec

User adjusted the v1 exit spec mid-cycle (220 → 199 → 150) and asked for pane heights to survive indicator toggles + interval changes + page reloads.

`Chart.tsx` adds a `paneHeightsRef = useRef<Partial<Record<PaneKey, number>>>({})` Map and a `taiex.pane.heights.v1` localStorage mirror. Hydrated on mount (defensive try/catch JSON parse, ignores non-finite/zero/negative values, defaults applied per missing key). Each indicator effect's pane-create branch reads `paneHeightsRef.current[<key>] ?? DEFAULT_PANE_HEIGHTS[<key>]` instead of a hardcoded literal. Each indicator effect's cleanup branch snapshots `pane.getHeight()` BEFORE `chart.removePane(...)` and persists. A single `setInterval(2000)` polls all 5 panes (candle + 4 indicator) so user drag-resizes are captured even without a toggle event. Cleared on unmount. Lightweight-charts v5 API used: `chart.panes()`, `pane.getHeight()`, `pane.setHeight()` — there's no `setStretchFactor` in v5.

`trade_strat_v1` exit rules rewritten (per user clarification, NOT `v1_trade.md` verbatim):

| condition | rule | reason code |
|---|---|---|
| TP | profit ≥ 150 (was 220) | `TP` |
| SL | loss ≥ 60 | `SL` |
| 10m DMI flip | `-DI > +DI` closes LONG; `+DI > -DI` closes SHORT | `DI_FLIP_10M` |
| 30m MACD-falling | `macd[-2] > macd[-1]` closes LONG; mirror for SHORT | `MACD_DOWN_30M` |

The 3m exit-assist rule from V5 is REMOVED. `resolutions` becomes `["10m", "30m", "1d"]`. `Params.exit_di_threshold` deleted. Priority inside `_on_30m`: TP/SL → MACD-falling → entry eval (a TP-hit bar with falling MACD emits TP only).

188 tests as of V5.1.

### V5.2 / V5.3 — Discord rich embed (Traditional Chinese)

User wanted Discord notifications to carry the trade conditions inline so they don't have to check `/analysis`. Decision: enrich the existing `DiscordNotifier` rather than route through an external Hermes / Anthropic AI agent (zero AI cost, deterministic, ~50ms latency, no new infra). The `/insights/strategy` button on `/analysis` already covers AI-driven analysis on demand.

V5.2 (commit `24ef862`) added: display_name lookup via `@lru_cache(maxsize=64)` over `app.strategies.registry.get`, `_fmt_ind` 8-key snapshot renderer (`K54 D51  MACD+9 sig+7 hist+2  +DI33 -DI19 ADX27`), Asia/Taipei timestamp, signed pnl, exit_reason field, signal_id footer.

V5.3 (commit `76fd1c2`) made the embed Traditional Chinese throughout while keeping indicator names English per CLAUDE.md convention:

- Side translation: LONG→多單, SHORT→空單, EXIT→平倉, FLAT→空手.
- Field names: Symbol→商品, Resolution→週期, Price→價格, Time→時間, Strategy→策略.
- Description block synthesized in TC by the notifier:
  - OPEN: `進場訊號 — KD > 20 / MACD 翻正 / +DI > 21 且 +DI > -DI` (entry-gate summary; hardcoded for v1/v2; unknown strategies fall back to `進場條件達標`).
  - CLOSE: `出場訊號 — {translated reason}（損益 {±value} 點）`.
- Exit reason translation: TP→達到停利目標, SL→觸及停損, DI_FLIP_10M→`10 分鐘 DMI 翻轉 (-DI > +DI)`, MACD_DOWN_30M→`30 分鐘 MACD 下彎`, DI_FLIP→`3 分鐘 DMI 翻轉`. Unknown codes pass through verbatim.
- Footer: `signal #N` → `訊號 #N`.

The hub threads `signal_id` into DiscordNotifier the same way it already does for `InAppNotifier`.

`POST /admin/test-webhook` (V4) hooks the right-rail 通知遞送 panel's 測試發送 button. Synthetic signal payload omits `entry_ind` so test sends are visually distinct from real signals (no indicator block).

205 tests as of V5.3.

#### Operational gotcha — adding env keys to `.env`

`docker compose restart backend` does NOT re-read `env_file`. Container keeps the env vars baked in at creation. To pick up a newly-added env key (e.g. `DISCORD_WEBHOOK_URL`):

```sh
docker compose up -d --force-recreate backend
```

Verify the env var landed in the container:

```sh
docker compose exec backend env | grep DISCORD_WEBHOOK_URL
curl -s http://127.0.0.1:8000/status | python3 -m json.tool | grep discord
# notifiers.discord should be true after the recreate
```

The `_notifier_presence` helper in `app/api/routes/status.py:57-69` reads `settings.discord_webhook_url` via `get_settings()` (which is `@lru_cache`-cached). The frontend's `AlertLog.tsx` only renders the `測試發送` button when `/status` reports the channel as configured. So a missing env var after a restart shows up as a missing test button.

#### Verification quick-reference

Backend health (during dev / live):

```sh
curl -s http://127.0.0.1:8000/status | python3 -m json.tool
# all of: ingest_running, strategy_loop_running, position_tracker_running, db_ok, ok → true
# notifiers.discord → true once .env's DISCORD_WEBHOOK_URL is loaded into the container
```

Confirm paper trading is live:

```sh
curl -s 'http://127.0.0.1:8000/strategies' | python3 -c "import json,sys; [print(f\"{s['name']:20s} enabled={s['enabled']}\") for s in json.load(sys.stdin)]"
curl -s 'http://127.0.0.1:8000/trades?limit=5' | python3 -m json.tool | head -50
curl -s 'http://127.0.0.1:8000/trades/stats?strategy=trade_strat_v1' | python3 -m json.tool
```

Old trades pre-V5 carry empty `payload: {}` — they predate the indicator-snapshot threading and won't be backfilled. New trades from V5 onward carry full `entry_ind` / `exit_ind` payloads.
