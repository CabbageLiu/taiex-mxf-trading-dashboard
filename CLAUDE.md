# CLAUDE.md

Guide Claude Code for this repo. Operator runbook + strategy authoring + Discord embed copy + deferred backlog + verification scripts live in `NOTES.md`.

## Commands

### Dev stack (recommended)

```sh
docker compose up           # db + backend + frontend, hot-reload all three
docker compose up --build   # first run, or after dependency changes
docker compose down         # clean stop (data preserved)
docker compose down -v      # also nuke ticks/signals/alerts/trades
```

Browser → `http://localhost:3000`. Backend `:8000`. DB `:5432`. Source bind-mounted; both servers hot-reload.

**On `frontend/package.json` or `backend/pyproject.toml` change**, plain `up` not enough. `/app/node_modules` + `/app/.venv` are anonymous volumes Compose v2 preserves by stable hash → stale volume masks new deps. Run:

```sh
docker compose stop {service} && docker rm -v taiex-{service} && docker compose up -d --build {service}
```

### Host workflow (no Docker)

```sh
docker compose up -d db                                  # TimescaleDB on 127.0.0.1:5432
cd backend
uv sync --extra dev
uv run alembic upgrade head                              # schema + continuous aggregates
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

uv run pytest -q                                         # all tests
uv run pytest tests/test_indicators.py                   # one file
uv run pytest -k "macd"                                  # match by name
uv run ruff check .                                      # lint

uv run alembic revision -m "msg" --autogenerate          # new migration
```

New alembic migration **must** load `target_metadata = Base.metadata` from `app.db.models` (wired in `app/db/migrations/env.py`). Continuous aggregates + Timescale extension = raw SQL inside migration body — autogenerate cannot produce; hand-edit.

Frontend: `cd frontend && npm install && npm run dev`. Typecheck: `npx tsc --noEmit`. Build: `npm run build`.

### Tailscale share

```sh
tailscale up
tailscale serve --bg --https=443 http://127.0.0.1:3000
tailscale serve --https=443 off               # stop
```

Both servers bind 127.0.0.1. `next.config.mjs` rewrites `/api/*` + `/ws/*` to FastAPI on `127.0.0.1:8000` → single proxy entry covers REST + WebSocket.

## Architecture

### Data flow (one process, asyncio)

```
FinMind 5-sec → MarketDataAdapter (adapters/finmind_taiex.py)
                       ↓ Tick(ts, symbol, price, source)
                IngestRunner (ingest/runner.py)
                       ├─► UPSERT ticks hypertable (ON CONFLICT DO NOTHING)
                       └─► fan-out per resolution: bar_update + bar_close
                              ├─► WebSocket /ws/stream (api/ws.py)
                              └─► StrategyLoop → Strategy.on_bar()
                                      → Signal persisted → NotifierHub
                                            ├─► DiscordNotifier
                                            ├─► N8nNotifier
                                            └─► InAppNotifier ─► WS + PositionTracker
                                                  (alerts row per attempt)
PositionTracker (runner/position_tracker.py)
   subscribes to InAppNotifier queue. Pairs LONG↔EXIT/SHORT into trades rows
   with pnl_points. Idempotent on signal id. Rehydrates open positions on startup.
```

**Ingest runner = single source of bar events.** Strategies + WebSocket subscribe via `IngestRunner.subscribe(resolution)`. Bar timing in `ingest/runner.py:_bucket_start`.

**InAppNotifier queue = single source of signal events.** WS endpoint + position tracker both subscribe. New signal consumer → `hub.inapp.subscribe()`; do NOT add a parallel queue. Hub threads `signal_id` through inapp payload so consumers attribute back to `signals.id`.

`IngestRunner` watchdog (`_watchdog_loop` + `_watchdog_tick`) fires every 5s + force-closes any `_open_buckets[res]` older than `3 × RESOLUTION_DELTAS[res]`. Tombstone set `_closed_buckets` (4 entries / resolution) blocks delayed ticks from re-seeding force-closed bucket → prevents double `bar_close` emits.

### TimescaleDB schema

`ticks` = only hypertable. Bars derived:

- **1m, 2m, 3m, 5m, 10m, 15m, 30m, 1h, 4h, 12h, 1d** — Timescale continuous aggregates w/ `add_continuous_aggregate_policy` running every 30 s.
- **1w, 1mo** — plain views on `bars_1d` (cagg-of-cagg restricted).

`/bars` (`app/api/routes/bars.py`) reads view per resolution. **No `bars` table.** Add new resolution = add to `RESOLUTIONS` in `ingest/runner.py`, `VALID_RES` in `api/routes/bars.py`, matching view in migration.

`trades` table (migration `0002_trades.py`) populated by position tracker. Not hypertable; partial unique index `ux_trades_open_position` on `(strategy, symbol) WHERE exit_ts IS NULL` prevents double-open race. FKs to `signals.id` use `ON DELETE SET NULL`.

### Indicator service

5 hand-rolled in `app/indicators/`: MA, MACD, RSI (Wilder), KD (TW-style 3-EMA smoothing), DMI (Wilder ADX). **No `pandas-ta`** — breaks on numpy 2 / pandas 3. `IndicatorCache` in `service.py` keys on `(symbol, resolution, kind, frozen-params)`, invalidates only when latest bar timestamp moves.

### Strategy plug-in framework

`Strategy` ABC + `BarEvent`/`Signal` dataclasses in `app/strategies/base.py`. Registry in `app/strategies/registry.py`:

1. **In-repo:** `discover()` walks `app/strategies/examples/`, picks up `@register_strategy`.
2. **External:** loads `taiex.strategies` Python entry-point group → strategy ships as separate pip package.

Strategy declares `resolutions: list[str]` + optional `indicator_specs: dict[label, {kind, params}]`; runner precomputes indicators, passes via `BarEvent.indicators`. Per-strategy `enabled`/`params`/`channels` live in `strategy_config` table; managed via `/strategies` REST.

- `name` ClassVar = canonical key for `trades.strategy`, `signals.strategy`, `strategy_config.name`.
- Optional `display_name: ClassVar[str | None]` for UI; UI renders `display_name ?? name`. Backend never accepts `display_name` as input.
- Optional `dump_state(symbol)` exposed at `GET /strategies/{name}/state`.

Strategies recreated per `bar_close` → state lives in module-level **`_STATE: dict[(name, symbol), _StratState]`**. Backtest engine snapshots + restores by introspection. **Convention is `_STATE`-named** — brittle to non-`_STATE` naming; document in any new strategy template.

Strategies may optionally override `on_tick(TickEvent)` for intra-bar firing. Two ClassVars control routing:

- `tick_resolutions: list[str]` — subset of `resolutions` that route to `on_tick` (raw `bar_update`, ~5s cadence) instead of `on_bar` (closed-bucket boundaries). Default `[]` preserves bar_close behaviour. The `_on_bar_close` filter excludes any resolution in this list, so a strategy that opts in cannot double-fire from both paths on a boundary tick.
- `aux_indicator_specs: dict[label, {kind, params, resolution}]` — auxiliary cross-resolution indicators. Framework loads bars + computes indicator inline on every dispatch, merging into `ev.indicators` under `label`. Avoids cross-task races / cold-start gaps / staleness windows that a strategy-side cache would expose.

When `on_tick` is overridden, `Signal.ts` carries tick precision (not bucket-aligned), and `signals.ts` / `trades.entry_ts` / `trades.exit_ts` reflect actual fill time. All three live strategies (`strat_30k`, `strat_15k`, `strat_1k`) are now tick-driven on their primary resolution and use `aux_indicator_specs` to read 5m MACD as an entry confirmation gate. `trade_strat_v1` / `trade_strat_v2` remain bar_close.

`in_entry_window(ts, tz)` (in `app.strategies.base`) returns True iff Taipei-local time falls in `[09:15, 12:15) ∪ [21:00, 24:00)` — half-open intervals, strict midnight cutoff (overnight 00:00–05:00 NOT in the window even though TAIFEX night session continues there). The 12:15–21:00 stretch (TAIFEX day-session close + first six hours of the night session) is closed for entries even though the market reopens at 15:00. All three live strategies gate entries on this window; **exits run anytime** so an open position is always closeable across the 12:15–21:00 closed gap.

Position tracker pairs LONG/SHORT/EXIT/FLAT into trades. Same-direction = no-op; opposite-direction atomically closes + opens at same price/timestamp; same-id replays idempotent. **Strategies emitting only `LONG` never close a trade** → never contribute to win-rate or PnL. Truth table + worked example in `NOTES.md` §7.

`Signal.payload` carries `entry_ind` (open) + `exit_ind` (close) — 8-key snapshot `{k, d, macd, signal, hist, plus_di, minus_di, adx}` rounded to 2 decimals, NaN → None. `_close` merges via `payload || jsonb_build_object('exit_ind', :exit_ind)` so existing `entry_ind` preserved.

**Fill convention deviation:** signals fire on bar close (framework limit). Spec calls for next-bar-open; deferred (`NOTES.md` §16).

In-repo strategies (live): `strat_30k`, `strat_15k`, `strat_1k` — single-resolution MA120-trend / KD / MACD / DMI strategies (`NOTES.md` §11.2). Legacy `trade_strat_v1` (30m, §11.1 + §12) and `trade_strat_v2` (5m, §11.1) stay registered but disabled by default.

### REST routes

Source: `app/api/routes/`. Quick map:

- `/trades`, `/trades/stats`, `/signals`, `/alerts/stats`, `/strategies`, `/strategies/{name}/state`, `/status`.
- `/insights/strategy` (POST) — 503 when `TAIEX_ANTHROPIC_API_KEY` unset; 5/min/(strategy, ip) bucket.
- `/admin/backfill` (POST) — historical tick backfill; 503 when `FINMIND_TOKEN` unset.
- `/admin/test-webhook?channel=discord|n8n` (POST) — fires synthetic Signal.
- `/backtest/run` (POST + GET) — share LRU keyed `(strategy, params_hash, symbol, start, end, module_mtime)`; `module_mtime` invalidates cache after edit.
- `/bars` returns CLOSED buckets only; in-progress bar comes from WebSocket.

`compute_stats` in `routes/trades.py` extracted as pure function so tests skip DB. Drawdown reported as positive magnitude (peak − cum); UI negates.

### Notifier hub

`NotifierHub.dispatch()` runs notifiers in `asyncio.gather`, writes one `alerts` row per attempt. Per-notifier failure caught — one bad webhook never starves others. `InAppNotifier` is in-process — operators see signals in dashboard even when webhooks misconfigured.

`DiscordNotifier` rich embed Traditional Chinese. Side LONG→多單 / SHORT→空單 / EXIT→平倉 / FLAT→空手. Exit reason codes: TP / SL / DI_FLIP_10M / MACD_DOWN_30M / DI_FLIP. Footer `訊號 #N`, Asia/Taipei timestamp. Translation table + entry-description copy: `NOTES.md` §14.

### Backtest engine

`app/backtest/engine.py` replays registered strategy across closed historical bars. Pine-Script-style return `{strategy, symbol, start, end, params, resolutions, bar_counts, signals[], trades[], stats, equity_curve[]}`.

- `load_bars` per declared resolution (reuses `/bars` cutoff that excludes in-progress bucket).
- Indicators precomputed via `indicator_cache.get` (warm across param sweeps).
- Schedule interleaves `bar_close` from all resolutions chronologically, **smaller-resolution-first tie-break** (5m fires before containing 30m on shared boundary).
- `_swap_state` / `_restore_state` snapshot module's `_STATE[(name, symbol)]` so backtests cannot pollute live state.
- `pair_into_trades` = pure function mirroring `PositionTracker`.
- `compute_backtest_stats` reuses `compute_stats` via `SimpleNamespace` adapters; adds `profit_factor`, `largest_win`, `largest_loss`, `avg_bars_in_trade`.

Engine fills at signal-bar close. No commission / slippage / position sizing / next-bar-open mode (`NOTES.md` §16).

### Frontend

Next.js 15 App Router, single locale `zh-Hant-TW`. `lib/i18n.ts` = tiny dict + `t()` helper. **Indicator names stay English** by design — never wrap in `t()`.

Routes (shared layout `app/layout.tsx` → `ShellHeader.tsx`):

- **`/trading`** — TopBar (resolution + StrategySelector + IndicatorToggleBar + MarkerFilterPills + refresh) + Chart + AlertLog right rail.
- **`/analysis`** — KPI strip + TradeFilterBar + TradesTable + TradeInsightPanel. `compare=1` → two `useBacktest` calls render side-by-side.
- **`/backtest`** — server-side `redirect("/analysis?compare=1&s=trade_strat_v1&s2=trade_strat_v2")`.
- `app/page.tsx` → server-side `redirect("/trading")`.

**Lens** (`frontend/lib/lens.ts`): URL params `?s=&s2=&start=&end=&res=&ind=&compare=` source of truth; `localStorage` key `taiex.lens.v1` mirrors them. `ShellHeader` nav links forward current querystring. **Anything reading `useSearchParams` must wrap in `<Suspense>` for Next 15 prerender.**

**Chart** (`components/Chart.tsx`, TradingView Lightweight Charts v5):

- True panes via `chart.addPane()` + `chart.addSeries(..., paneIndex)`; MACD / RSI / KD / DMI each own pane (MA stays on price pane).
- **TW candle convention: red = up 漲 (#c0392b), green = down 跌 (#3a7d4f)** — opposite of US. Histograms + DMI lines follow.
- `/bars` returns CLOSED only — WebSocket = sole source of in-progress bar. `lastBarRef` authoritative; `prevResRef` clears on resolution change so stale 1m bar cannot leak into 5m series.
- `ChartCrosshairTooltip.tsx` reads `Map<time, values>` populated alongside `series.setData` — **no network call on hover**. When toggling indicators, map must be cleared/rebuilt for affected series.
- Time axis via `Intl.DateTimeFormat({ timeZone: "Asia/Taipei", locale: "zh-Hant-TW" })`. **Don't pass UTC-shifted epoch seconds** — breaks crosshair lookups (map keys = original UTC `time`).
- Markers via `createSeriesMarkers` (idempotent on `signal.id`). Entry / TP / SL price lines via `series.createPriceLine`. Pane heights persist in `paneHeightsRef` + localStorage `taiex.pane.heights.v1`.
- `lib/ws.ts` derives WS URL from `window.location` (not env var) → same code works on `127.0.0.1:3000` + Tailscale Serve.

**Queries / types** — `lib/queries.ts` = single home for TanStack Query hooks (`useStatus`, `useTrades`, `useTradeStats`, `useInsight`, `useBacktest`, `useStrategies`, `useStrategyState`). Components import from there rather than calling `fetch` directly. `lib/api.ts` defines `TradePayload` 8-key snapshot.

**Trades table** — columns 編號 / 策略 (`display_name ?? name`, `title=canonical`) / 開倉指標 / 出場指標 (rendered via `formatIndicators` as `K54 D51 / MACD+9 / +DI33 -DI19`). Pre-V5 trades render `—`. Header copy TC; indicator names stay English.

### Configuration

`app/config.py` uses `pydantic-settings` w/ `env_file=("../.env", ".env")` so commands work from `backend/` or repo root. `@lru_cache` on `get_settings`.

- **Display vs source symbol:** `SYMBOL_DISPLAY=MXF` decoupled from `SYMBOL_SOURCE=TXF`. **FinMind sponsor `taiwan_futures_snapshot` serves TXF/TMF/CDF only** — `data_id=MXF` returns 0 rows + silently freezes feed. TXF + MXF track same TAIEX index, so labelling fine.
- **AI insights env alias:** `anthropic_api_key` reads `TAIEX_ANTHROPIC_API_KEY` (prefixed to avoid clobbering Claude Code's `ANTHROPIC_API_KEY` when shell sources `.env`). Optional; `POST /insights/strategy` returns 503 when unset + frontend AI panel degrades cleanly.
- **TAIFEX session:** day 08:45–13:45 Taipei, night 15:00 evening start (Mon-Fri) → 05:00 overnight (Tue-Sat). Sun closed. Override via `NIGHT_SESSION_OPEN`/`NIGHT_SESSION_CLOSE`. `_market_open` evaluates day OR night-evening OR night-overnight.

### Data quality (ingest boundary)

Shared `app.ingest.constants.PRICE_FLOOR = 1000.0`:

1. **Zero-priced live ticks** — `_rows_to_ticks` drops `price < PRICE_FLOOR`.
2. **Calendar-spread quotes** — `FinmindHistoricalClient.fetch_day` rejects rows where `'/' in contract_date` + sub-floor guard.
3. **Multi-contract pollution** — `_pick_front_month` per poll: prefer `futures_id` ending `R1`, else highest `total_volume`, tie-break smallest numeric `contract_date`. Backfill applies analogous filter (count `contract_date`, keep most-traded; empty rows free pass + warning).

Recovery scripts in `backend/scripts/` — `purge_zero_ticks.py` (bulk-clean sub-floor rows + cagg refresh) and `wipe_and_rebackfill.py` (`TRUNCATE ticks` + cagg refresh; **destructive, no env guard, dev container only**). Both use separate `AUTOCOMMIT` connection for `CALL refresh_continuous_aggregate` (Timescale rejects them inside transaction).

### AI insights service

`app/services/insights.py` calls Sonnet 4.6 via Anthropic SDK with prompt caching (`cache_control: ephemeral` on system prompt). User message JSON-encodes trade payload — never f-string interpolated — so malicious `Signal.payload.reason` cannot break out of JSON + inject. **When editing system prompt, keep `cache_control` marker on the *last* system content block + prompt as a true module-level constant** — any byte change (incl. `datetime.now()` interpolation) silently invalidates the prefix cache.

Compare mode appends a second `cache_control: ephemeral` system block (`COMPARE_SYSTEM_TAIL`); original `SYSTEM_PROMPT` constant stays byte-unchanged so live-mode prefix cache survives.

`insights_cache.py` = bounded TTL+LRU on `OrderedDict`, monotonic-time. Restart drops cache. Rate limit 5/min/(strategy, ip) honours `X-Forwarded-For` — collapses to single bucket behind reverse proxy that strips it (`NOTES.md` §16).

## Tests

~205 tests as of V5.3. Run: `cd backend && uv run pytest -q | tail -1`.

**No tests require live DB.** Mock at `session_scope()` boundary or extract SQL into a pure function (e.g. `compute_stats` — SQL part in `_query_trades`, math separate).

## Operational gotcha — env_file

`docker compose restart backend` does NOT re-read `env_file`. Container keeps env vars baked in at creation. To pick up newly-added env keys:

```sh
docker compose up -d --force-recreate backend
docker compose exec backend env | grep KEY
curl -s http://127.0.0.1:8000/status | python3 -m json.tool
```

`_notifier_presence` (`app/api/routes/status.py:57-69`) reads `settings.discord_webhook_url` via `@lru_cache`-cached `get_settings()`. Frontend `AlertLog.tsx` only renders 測試發送 button when `/status` reports channel as configured.

## Pointers

- Operator runbook (daily start/stop, troubleshooting, AI insights setup, backfill): `NOTES.md` §0–§10.
- Strategy authoring guide + worked long↔short pair example + `BarEvent`/`Signal`/`indicator_specs`/`params_schema` API: `NOTES.md` §7.
- `trade_strat_v1` / `trade_strat_v2` full spec: `NOTES.md` §11.1, §12.
- Discord embed copy + reason translation table: `NOTES.md` §14.
- Deferred backlog: `NOTES.md` §16.
- Verification quick-reference (curl recipes): `NOTES.md` §17.
