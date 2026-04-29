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

### Tests

`backend/tests/` covers indicator math (against straight-uptrend fixtures), notifier hub fan-out + per-channel failure isolation + channel filter, FinMind adapter dedupe + invalid-row tolerance, V2 position tracker (open/close/flip/idempotency/rehydrate), V2 trades API (`compute_stats` win rate / drawdown / avg hold extracted as a pure function specifically so tests can call it without DB), V2 insights cache (TTL + LRU + key sensitivity), V2 insights service (system-prompt persona, `cache_control` marker, JSON-encoded payload escapes a known prompt-injection string), and V2.5 backfill (trading-day filter, FinMind client parse + quota path, `_missing_days` threshold + today-skip, range/recent flows). **57 tests as of V2.5.**

**None of the tests require a live database.** When adding a feature that needs DB I/O, mock at the `session_scope()` boundary or extract the SQL-touching logic into a pure function the test can patch directly (this is what `compute_stats` does — the SQL part is in `_query_trades`, the math is separate).

### Backlog and security

V2 + V2.5 deferred several gaps to V3 (documented in `V3_plan.md`): CORS still wide open, mutating endpoints unauthenticated (`/strategies/*`, `/insights/strategy`, `/admin/backfill`), no global Anthropic spend cap, reverse-proxy IP gap on the rate limiter, no strategy replay over backfilled bars (the data is now available; the replay layer is not), no TW holiday calendar (V2.5 backfill iterates Mon-Fri including holidays — wasted API calls), `/admin/backfill` synchronous (multi-month windows block; V3 to make it streaming or background), no per-trade fees/slippage, no auth/multi-user. **Do not silently re-introduce these as if they were new ideas — check `V3_plan.md` first.** When working on V3 items, that file is the canonical scope reference.
