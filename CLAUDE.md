# CLAUDE.md

Guide Claude Code for this repo.

## Commands

### Dev stack (recommended)

```sh
docker compose up           # db + backend + frontend, hot-reload all three
docker compose up --build   # first run, or after dependency changes
docker compose down         # clean stop (data preserved)
docker compose down -v      # also nuke ticks/signals/alerts/trades
```

Browser → `http://localhost:3000`. Backend `:8000`. DB `:5432`. Source bind-mounted; both servers hot-reload.

**On `frontend/package.json` or `backend/pyproject.toml` change**, plain `up` not enough. `/app/node_modules` + `/app/.venv` mounts = anonymous volumes Compose v2 preserves by stable hash → stale volume masks new deps. Run:

```sh
# frontend
docker compose stop frontend && docker rm -v taiex-frontend && docker compose up -d --build frontend

# backend
docker compose stop backend && docker rm -v taiex-backend && docker compose up -d --build backend
```

### Host workflow (no Docker)

```sh
docker compose up -d db          # TimescaleDB on 127.0.0.1:5432, volume taiex-pg
docker compose ps                # wait for STATUS=healthy
docker compose stop              # keep data
docker compose down -v           # nuke ticks/signals/alerts/trades
```

Backend (uv-managed; venv in `backend/.venv`):

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

New alembic migration **must** load `target_metadata = Base.metadata` from `app.db.models` (wired in `app/db/migrations/env.py`). Continuous aggregates + Timescale extension = raw SQL inside migration body — autogenerate cannot produce; hand-edit.

Frontend:

```sh
cd frontend
npm install
npm run dev                                   # binds 127.0.0.1:3000
npx tsc --noEmit                              # typecheck only
npm run build                                 # production build
```

### Tailscale share

```sh
tailscale up
tailscale serve --bg --https=443 http://127.0.0.1:3000
tailscale serve status                         # prints public-on-tailnet URL
tailscale serve --https=443 off                # stop
```

Both servers bind 127.0.0.1. `next.config.mjs` rewrites `/api/*` + `/ws/*` to FastAPI on `127.0.0.1:8000` → single proxy entry covers REST + WebSocket.

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

PositionTracker (runner/position_tracker.py)
   subscribes to InAppNotifier queue (same fan-out as WS — one source of
   signal events, no parallel queue). Pairs LONG↔EXIT/SHORT into rows in
   `trades` table with `pnl_points`. Idempotent on signal id.
   Rehydrates open positions from DB on startup.
```

**Ingest runner = single source of bar events.** Strategies + WebSocket subscribe to in-process `asyncio.Queue` fan-out via `IngestRunner.subscribe(resolution)`. Bar timing derived from tick `ts` rounded into resolution buckets in `ingest/runner.py:_bucket_start`.

**InAppNotifier queue = single source of signal events.** WebSocket endpoint + position tracker both subscribe. New signal consumer → call `hub.inapp.subscribe()`; do NOT add parallel queue. Hub threads `signal_id` through inapp payload so consumers attribute back to `signals.id`.

### TimescaleDB schema

`ticks` = only hypertable holding raw data. Bars derived:

- **1m, 2m, 3m, 5m, 10m, 15m, 30m, 1h, 4h, 12h, 1d** — Timescale **continuous aggregates** w/ `add_continuous_aggregate_policy` running every 30 s.
- **1w, 1mo** — plain views on `bars_1d` (cagg-of-cagg restricted).

`/bars` (`app/api/routes/bars.py`) reads view matching requested resolution. **No `bars` table.** Add new resolution = add to `RESOLUTIONS` in `ingest/runner.py`, `VALID_RES` in `api/routes/bars.py`, matching view in migration.

`trades` table (migration `0002_trades.py`) populated by position tracker. Not hypertable; partial unique index `ux_trades_open_position` on `(strategy, symbol) WHERE exit_ts IS NULL` prevents double-open race. FKs to `signals.id` use `ON DELETE SET NULL`.

### Indicator service

Five hand-rolled indicators in `app/indicators/`: MA, MACD, RSI (Wilder), KD (TW-style 3-EMA smoothing), DMI (Wilder ADX). **No `pandas-ta`** — breaks on numpy 2 / pandas 3. `IndicatorCache` in `service.py` keys on `(symbol, resolution, kind, frozen-params)`, invalidates only when latest bar timestamp moves.

### Strategy plug-in framework

`Strategy` ABC + `BarEvent`/`Signal` dataclasses in `app/strategies/base.py`. Registry in `app/strategies/registry.py`:

1. **In-repo:** `discover()` walks `app/strategies/examples/`, picks up `@register_strategy` decorators.
2. **External:** loads `taiex.strategies` Python entry-point group → strategy ships as separate pip package.

Strategy declares `resolutions: list[str]` + optional `indicator_specs: dict[label, {kind, params}]`; runner precomputes indicators, passes via `BarEvent.indicators`. Per-strategy `enabled`, `params`, `channels` live in `strategy_config` table; managed via `/strategies` REST.

Position tracker pairs LONG/SHORT/EXIT/FLAT into `trades` rows. Same-direction = no-op; opposite-direction atomically closes + opens at same price/timestamp; same-id replays idempotent. **Strategies emitting only `LONG` never close a trade** → never contribute to win-rate or PnL. Pairing truth table + worked example in `NOTES.md` §7.

`Strategy` ABC has optional `display_name: ClassVar[str | None]` (UI rendering) + optional `dump_state(symbol) -> dict` classmethod. DB-bound `name` ClassVar = canonical key for `trades.strategy`, `signals.strategy`, `strategy_config.name`. UI renders `display_name ?? name`. Backend never accepts `display_name` as input on `/strategies/{name}/*`. `GET /strategies/{name}/state` exposes dump_state snapshot.

Strategies recreated per `bar_close`, so position / cooldown state lives in module-level `_STATE: dict[(name, symbol), _StratState]`. Backtest engine snapshots + restores `_STATE` automatically (any module-level `_STATE: dict` keyed by `(strategy_name, symbol)` detected). **Convention is module-introspection-based** — brittle to non-`_STATE` naming; document in any new strategy template.

### Historical backfill

`app/ingest/backfill.py` fills gap between live `taiwan_futures_snapshot` (real-time only, no history) + closing-laptop-loses-data expectation. Hits FinMind `TaiwanFuturesTick` per market day, inserts via same `ON CONFLICT DO NOTHING` path live ingest uses (`source = "FINMIND_FUTURES_TICK"` distinguishes streams). Inserts chunked at 5000 rows/query (Postgres caps single statement at 65,535 bind params; full day of `MTX` ticks = 200k+ rows).

Two entry points: `BackfillService.backfill_range(start, end)` (manual, `POST /admin/backfill?start=&end=`) + `BackfillService.backfill_recent(lookback_days)` (auto-fired in lifespan; `BACKFILL_ON_STARTUP_DAYS=0` to disable). Today always skipped — TaiwanFuturesTick updates end-of-day.

### Data quality

Three pollution paths fixed at ingest boundary; shared `app.ingest.constants.PRICE_FLOOR = 1000.0`:

1. **Zero-priced live ticks.** `taiwan_futures_snapshot` occasionally returns `close: 0`. `_rows_to_ticks` drops `price < PRICE_FLOOR`.
2. **Calendar-spread quotes.** `TaiwanFuturesTick` mixes outright single-leg trades w/ TAIFEX combo orders (`contract_date='202604W5/202605'`, price ~86–700). `FinmindHistoricalClient.fetch_day` rejects rows where `'/' in contract_date` + sub-floor guard.
3. **Multi-contract pollution.** Snapshot returns ALL expiries (TXFE6 front, TXFR1 rolling-front alias, TXFR2 next-week, back-months, far-month at +1000pt carry premium). `_pick_front_month` picks one per poll: prefer `futures_id` ending `R1`, else highest `total_volume`, tie-break smallest numeric `contract_date`. Backfill applies analogous filter — count `contract_date` values, keep most-traded per day. Empty `contract_date` rows get free pass (legacy compat); logs warning when can't discriminate.

### TAIFEX after-hours session

夜盤 runs `15:00` Taipei through `05:00` next morning (Mon-Fri evening start; Sat 00:00–05:00 belongs to Friday; Sat after 05:00 + all Sunday closed). Day session `08:45–13:45`.

`app/config.py` settings: `night_session_open=15:00`, `night_session_close=05:00` (override via `NIGHT_SESSION_OPEN` / `NIGHT_SESSION_CLOSE`). `FinMindTaiexAdapter._market_open` evaluates day OR night-evening (Mon-Fri ≥15:00) OR night-overnight (Tue-Sat ≤05:00). `_next_open` walks forward minute-by-minute (bounded 4 days).

### Recovery scripts

`backend/scripts/`:

- `purge_zero_ticks.py` — deletes `WHERE price < PRICE_FLOOR`, refreshes all continuous aggregates. Use after ingest fixes when bulk data good.
- `wipe_and_rebackfill.py` — `TRUNCATE ticks` + cagg refresh; relies on next backend restart to re-run auto-backfill. Destructive — no env guard, run inside dev container only.

Both use separate `AUTOCOMMIT` connection for `CALL refresh_continuous_aggregate` (Timescale rejects them inside transaction).

### REST routes

- `GET /trades?strategy=&start=&end=&result=win|loss|all&limit=` — date-only `end` interpreted as start-of-next-day exclusive.
- `GET /trades/stats?strategy=&start=&end=` — `{trade_count, open_count, win_count, loss_count, win_rate, pnl_total, pnl_avg_win, pnl_avg_loss, max_drawdown, avg_hold_seconds}`. Drawdown reported positive magnitude (peak − cum); UI negates.
- `GET /status` — `{ok, ingest_running, last_tick_ts, ingest_lag_seconds, strategy_loop_running, position_tracker_running, db_ok, notifiers: {discord, n8n, inapp}}`.
- `POST /insights/strategy` — body `{strategy, start, end, filter}`, returns `{cached, generated_at, content}`. 503 when `ANTHROPIC_API_KEY` unset; 429 rate-limited (`Retry-After` header).
- `POST /admin/backfill?start=YYYY-MM-DD&end=YYYY-MM-DD` — historical tick backfill. 503 when `FINMIND_TOKEN` unset.
- `POST /backtest/run` (body) + `GET /backtest/run` (query, idempotent) — share LRU keyed on `(strategy, params_hash, symbol, start, end, module_mtime)`. `module_mtime` of strategy module file invalidates cache after edit.
- `GET /signals?strategy=&since=&limit=` — seeds 即時訊號 panel on mount.
- `GET /alerts/stats` — per-channel `{channel: {sent, failed, last_ts}}`, powers channel chips.
- `POST /admin/test-webhook?channel=discord|n8n` — fires synthetic Signal. 503 if env unset, 200 `{ok}` on success.

### Notifier hub

`NotifierHub.dispatch()` runs all configured notifiers in `asyncio.gather`, writes one `alerts` row per attempt. Failures caught per-notifier; one bad webhook never starves others. `InAppNotifier` in-process, publishes onto queue WS subscribes to → operators see signals in dashboard even when webhooks misconfigured.

`DiscordNotifier` rich embed (Traditional Chinese):

- Side: LONG→多單, SHORT→空單, EXIT→平倉, FLAT→空手.
- Fields: Symbol→商品, Resolution→週期, Price→價格, Time→時間, Strategy→策略.
- Description (synthesized by notifier):
  - OPEN: `進場訊號 — KD > 20 / MACD 翻正 / +DI > 21 且 +DI > -DI` (hardcoded for v1/v2; unknown strategies fall back to `進場條件達標`).
  - CLOSE: `出場訊號 — {translated reason}（損益 {±value} 點）`.
- Exit reason translation: TP→達到停利目標, SL→觸及停損, DI_FLIP_10M→`10 分鐘 DMI 翻轉 (-DI > +DI)`, MACD_DOWN_30M→`30 分鐘 MACD 下彎`, DI_FLIP→`3 分鐘 DMI 翻轉`. Unknown codes pass through verbatim.
- Footer: `訊號 #N`. Asia/Taipei timestamp; signed pnl. Test-webhook payload omits `entry_ind` so test sends visually distinct.
- display_name lookup via `@lru_cache(maxsize=64)` over `app.strategies.registry.get`.
- `_fmt_ind` 8-key snapshot renderer (`K54 D51  MACD+9 sig+7 hist+2  +DI33 -DI19 ADX27`).

### Configuration

`app/config.py` uses `pydantic-settings` w/ `env_file=("../.env", ".env")` → same `.env` at repo root works whether commands run from `backend/` or root. Cached via `@lru_cache` on `get_settings`.

Display symbol (`SYMBOL_DISPLAY`, default `MXF`) decoupled from source (`SYMBOL_SOURCE`, default `TXF`) — adapter labels every tick w/ `symbol_display`. **FinMind sponsor `taiwan_futures_snapshot` serves `TXF / TMF / CDF` only;** `data_id=MXF` returns zero rows + silently freezes feed. TXF + MXF both track same TAIEX index, so labelling TXF data as MXF in UI semantically fine.

Optional Anthropic settings: `anthropic_api_key: SecretStr | None`, `anthropic_model: str = "claude-sonnet-4-6"`, `insights_cache_ttl_seconds`, `insights_cache_max_entries`. When unset, `POST /insights/strategy` returns 503 + frontend AI panel degrades cleanly.

### AI insights service

`app/services/insights.py` calls Sonnet 4.6 via Anthropic SDK w/ prompt caching (`cache_control: ephemeral` on system prompt). User message JSON-encodes trade payload — never f-string interpolated — so malicious `Signal.payload.reason` cannot break out of JSON + inject. System prompt tells model to treat trade-row data as non-executable. **When editing system prompt, ensure `cache_control` marker stays on *last* system content block + prompt remains true module-level constant** — any byte change (incl. stray `datetime.now()` interpolation) silently invalidates prefix cache. Min cacheable prefix on Sonnet 4.6 = 2048 tokens; current prompt shorter so caching wired but no-op in practice.

Compare mode posts `compare_a` / `compare_b` payloads; backend appends second `cache_control: ephemeral` system block (`COMPARE_SYSTEM_TAIL`) — original `SYSTEM_PROMPT` constant stays byte-unchanged so live-mode prefix cache survives.

`app/services/insights_cache.py` = in-process bounded TTL+LRU on `OrderedDict`, monotonic-time. Key fingerprint hashes sorted `(trade_id, pnl_points)` tuples + filter. Restart drops cache.

`POST /insights/strategy` enforces 5/min/(strategy, ip) token bucket inside LRU dict capped 1024 keys. Honours `X-Forwarded-For`. Behind reverse proxy that strips it, limit collapses to single bucket.

### Backtest engine

`app/backtest/engine.py` replays registered strategy across closed historical bars. Returns Pine-Script-style `{strategy, symbol, start, end, params, resolutions, bar_counts, signals[], trades[], stats, equity_curve[]}`.

- `load_bars` per declared resolution (reuses `/bars` cutoff that excludes in-progress bucket).
- Indicators precomputed via `indicator_cache.get` (warm across param sweeps).
- Schedule interleaves bar_close from all resolutions chronologically, smaller-resolution-first tie-break (5m fires before containing 30m on shared boundary).
- `_swap_state` / `_restore_state` snapshot module's `_STATE[(name, symbol)]` so backtests cannot pollute live state.
- `pair_into_trades` = pure function mirroring `PositionTracker`.
- `compute_backtest_stats` reuses `app.api.routes.trades.compute_stats` via `SimpleNamespace` adapters + adds `profit_factor`, `largest_win`, `largest_loss`, `avg_bars_in_trade`.

Engine fills at signal-bar close (no `next_bar_open` mode), no commission / slippage / position sizing.

### Frontend

Next.js 15 App Router, single locale (`zh-Hant-TW`). `lib/i18n.ts` = tiny dict + `t()` helper. **Indicator names stay English** by design — never wrapped in `t()`.

Routes under shared layout (`app/layout.tsx` → `ShellHeader.tsx` w/ brand + nav + status pill):

- **`/trading`** — `app/trading/page.tsx`. TopBar (resolution + StrategySelector combobox + IndicatorToggleBar + MarkerFilterPills + refresh) + Chart + AlertLog right rail.
- **`/analysis`** — `app/analysis/page.tsx`. KPI strip + TradeFilterBar + TradesTable + TradeInsightPanel. Lens window selected → `useBacktest()` feeds KPI + table. `compare=1` → two `useBacktest` calls run parallel + page renders side-by-side `1fr 1fr`.
- **`/backtest`** — server-side `redirect("/analysis?compare=1&s=trade_strat_v1&s2=trade_strat_v2")` (Next 15 idiomatic).
- `app/page.tsx` = server-side `redirect("/trading")`.

#### Lens (global state)

`frontend/lib/lens.ts` = canonical hook. URL params `?s=&s2=&start=&end=&res=&ind=&compare=` = source of truth; `localStorage` key `taiex.lens.v1` mirrors them + seeds on cold mount when URL has no params. `s` = primary, `s2` = comparison, `start`/`end` = ISO dates, `res` = chart resolution, `ind` = comma-joined indicator allow-list. Both `/trading` + `/analysis` consume same hook. `ShellHeader` nav links forward current querystring. Anything reading `useSearchParams` must wrap in `<Suspense>` for Next 15 prerender.

#### Chart

`components/Chart.tsx` uses **TradingView Lightweight Charts v5**. True panes via `chart.addPane()` + `chart.addSeries(..., paneIndex)`; MACD / RSI / KD / DMI each own pane (MA stays on price pane). **TW candle convention: red = up 漲 (#c0392b), green = down 跌 (#3a7d4f)** — opposite of US. Histograms + DMI lines follow same colour grammar. CSS strategy color tokens `--strategy-1` / `--strategy-2` disambiguate primary vs comparison.

`/bars` returns ONLY closed historical buckets — WebSocket = sole source of in-progress bar. `lastBarRef` authoritative for in-progress bucket; bars-effect re-overlays after `setData(history)` if `time` strictly newer than last historical bar. `prevResRef` clears `lastBarRef` on resolution change so stale 1m bar cannot leak into 5m series. `refetchInterval` on `useQuery(['bars',res])` = `300_000` (5min) — recovery mechanism, not primary update path.

`IngestRunner` watchdog (`_watchdog_loop` + `_watchdog_tick`) fires every 5s + force-closes any `_open_buckets[res]` older than `3 × RESOLUTION_DELTAS[res]`. Tombstone set `_closed_buckets` (bounded 4 entries per resolution) blocks delayed ticks from re-seeding force-closed bucket → prevents double `bar_close` emits.

`ChartCrosshairTooltip.tsx` = React overlay on `chart.subscribeCrosshairMove`. Reads `Map<time, values>` populated alongside `series.setData`, patched on bar updates — **no network call on hover**. Cursor-price line via `series.coordinateToPrice`. When toggling indicators, map must be cleared/rebuilt for affected series.

Time on axis + tooltip via `Intl.DateTimeFormat({ timeZone: "Asia/Taipei", locale: "zh-Hant-TW" })` through `localization.timeFormatter` + `timeScale.tickMarkFormatter`. **Don't pass UTC-shifted epoch seconds** — breaks crosshair lookups (map keys = original UTC `time`).

Live updates: every `bar_update` WS message either appends new bar or extends in-progress (high/low/close mutate, open sticky). Bar history fetched once via `/api/bars` on resolution change; ongoing bars from WS only.

`lib/ws.ts` derives WS URL from `window.location` (not env var) → same code works on `127.0.0.1:3000` + Tailscale Serve hostname. `next.config.mjs` rewrite for `/ws/:path*` makes that work.

Chart overlays on price pane:
- **Markers** via `createSeriesMarkers(series, [])` — entry arrows (red `arrowUp` LONG below bar / green `arrowDown` SHORT above bar) + exit circles (red TP / green SL / accent DI flip). Idempotent on `signal.id` via `seenSignalIdsRef`. Live markers filled, backtest markers half-opacity dotted.
- **Entry / TP / SL price lines** via `series.createPriceLine(...)` — drawn on entry, torn down on exit. Entry grey dashed, TP red dotted, SL green dotted.
- **Entry→exit dashed connector** — single `LineSeries`, segments built from `useTrades({strategy, result: "all"})`, separated by whitespace data points (`{time}` no `value`).
- Exit-dot pane-relative y-clamp via `chart.paneSize(0).height` (defensive try/catch fallback to canvas height); when clamped, paints directional `▲` / `▼` chevron beside disc. Hover hit-test via `paintedCoordsRef: Map<TradeEvent, {x, y, outOfRange}>`. `window.__taiexMarkerStats` exposes `{events, drewOpen, drewClose, skipped, clamped, retryScheduled, hitTestable}` for browser-console debugging. 250ms retry retained.
- `MarkerFilterPills.tsx` in TopBar (全部 / per-strategy display name); `<Chart>` accepts `markerStrategies?: Set<string> | null` (null = all). Filters in `filteredTradeEvents` memo so paint + hover both inherit.
- `TradeMarkerTooltip` head row carries `#${tradeId}` chip (sumi-gold `.trade-marker-id`).

`HiLoBadge.tsx` top-left overlay subscribes `chart.timeScale().subscribeVisibleLogicalRangeChange`, reports high/low of *visible* range. `DailyConfidenceBadge.tsx` (right rail on `/trading`, not chart overlay) for `trade_strat_v1`: 多/空 0..3 dot rows + position summary. Polls `useStrategyState(name)` every 60s.

#### Pane heights

`paneHeightsRef = useRef<Partial<Record<PaneKey, number>>>({})` Map + `taiex.pane.heights.v1` localStorage mirror. Hydrated on mount (defensive try/catch JSON parse, ignores non-finite/zero/negative). Each indicator effect's pane-create branch reads `paneHeightsRef.current[<key>] ?? DEFAULT_PANE_HEIGHTS[<key>]`. Cleanup branch snapshots `pane.getHeight()` BEFORE `chart.removePane(...)` + persists. `setInterval(2000)` polls all 5 panes (candle + 4 indicator) so drag-resizes captured. Lightweight-charts v5 API: `chart.panes()`, `pane.getHeight()`, `pane.setHeight()` — no `setStretchFactor`.

#### Queries + types

`lib/queries.ts` = single home for TanStack Query hooks: `useStatus`, `useTrades`, `useTradeStats`, `useInsight` (mutation, manual trigger), `useBacktest`, `useStrategies`, `useStrategyState`. Components import from there rather than calling `fetch` directly.

`lib/api.ts` defines `TradeIndicators` + `TradePayload`; `Trade.payload` typed as `TradePayload` (8-key snapshot `{k, d, macd, signal, hist, plus_di, minus_di, adx}`).

#### Right rail / alerts

`AlertLog.tsx` renders `display_name ?? name` w/ `title={canonical_name}`. Click on 即時訊號 row dispatches `chart-scroll-to` `CustomEvent` chart on `/trading` listens for + pans timescale to bar.

通知遞送 panel shows channel chips from `/alerts/stats`, w/ 測試發送 button per channel — only renders when `/status` reports channel as configured (`_notifier_presence` reads `settings.discord_webhook_url` via `@lru_cache`-cached `get_settings()`).

#### Trades table

`TradesTable.tsx` columns: 編號 (`tr.id`), 策略 (display_name fallback to canonical, `title={tr.strategy}`), 開倉指標, 出場指標 — rendered via `formatIndicators` as `K54 D51 / MACD+9 / +DI33 -DI19`. Pre-V5 trades w/ empty payload render `—`. Header copy TC; indicator names (K, D, MACD, +DI, -DI) stay English.

#### Polish tokens

Typography scale tokens (`--fs-caption/meta/body/subhead/head/num-lg/section/display/num-xl`) replace hardcoded literals across `globals.css` + inline styles. Body 16 px, subhead 17 px. `--fw-semi: 600`, `--fw-bold: 700`. Spacing scale `--space-1..7`. Easing `--ease-out/in/spring`, motion `--dur-fast/base/slow`. Elevation tokens (`--shadow-sm/md/card/pop`). Focus rings via `:focus-visible`. Hover-lift on buttons + pills (honours `prefers-reduced-motion`). `Skeleton.tsx` w/ shimmer; `KpiCard`, `TradesTable` (`aria-busy` + sr-only caption), `TradeInsightPanel` accept `isLoading`.

Keyframes (transform/opacity only, w/ `prefers-reduced-motion: reduce` overrides):
- `fadeInUp` (`opacity 0→1, translateY(8px)→0`).
- `underlineGrow` (`scaleX(0)→1, transform-origin: left`).
- `pulseAccent` (one-shot ring, no infinite loop).

`.section-title` (Noto Serif TC, `--fs-section`, `--fw-bold`, accent rule via `::after` + `underlineGrow`) = canonical heading style. Applied to all panel headings. KPI card stagger via `:nth-child` `animation-delay: 0/40/80/120ms`. `font-variant-numeric: tabular-nums` on every price/PnL/timestamp column. Layout grid `1fr 340px` + TW candle palette unchanged.

Lucide-react icons replace unicode glyphs (`Settings` in `StrategySelector`, `RefreshCw` in TopBar). TopBar refresh button: 44×44 touch target, `aria-label="重新整理 K 線"`. Click invalidates `["bars", res]` + `["indicators"]` via `useQueryClient`; spins `.spinning` (`@keyframes spin`) til both refetches resolve, toggles `.pulse-success` on completion.

## Strategies

### `trade_strat_v1` — 30分鐘線策略

`resolutions = ["10m", "30m", "1d"]`. `display_name = "30分鐘線策略"`.

- **Entry (30m):** `KD > 20` AND `+DI > 21` AND `+DI > -DI`. MACD-histogram rising-edge gate: `hist[-3] <= 0 AND hist[-2] > 0 AND hist[-1] > hist[-2]` (evaluated on `macd["hist"]` column, NOT macd line). Symmetric SHORT mirror via `_macd_just_turned_positive(-macd["hist"])`.
- **Exit (30m bar close priority):**

| condition | rule | reason code |
|---|---|---|
| TP | profit ≥ 150 | `TP` |
| SL | loss ≥ 60 | `SL` |
| 10m DMI flip | `-DI > +DI` closes LONG; `+DI > -DI` closes SHORT | `DI_FLIP_10M` |
| 30m MACD-falling | `hist[-2] > hist[-1]` closes LONG; mirror SHORT (histogram) | `MACD_DOWN_30M` |

Priority inside `_on_30m`: TP/SL → MACD-falling → entry eval (TP-hit bar w/ falling MACD emits TP only).
- **Daily confidence (1d):** 0..3 long-side + 0..3 short-side condition counts. Display only — never blocks entry.
- **Discipline:** 1 contract no pyramiding, 5×30m-bar cooldown after exit, freshness filter on rising edge.

### `trade_strat_v2` — 5分鐘策略

`resolutions = ["1m", "3m", "5m", "1d"]`. `display_name = "5分鐘策略"`.

- **Entry (5m):** same condition shape as v1 but on 5m timeframe. MACD gates on histogram (`macd["hist"]`).
- **Exit-assist (3m):** `-DI ≥ 23` (note `>=` not `>` — distinct from v1) → emit EXIT.
- **TP/SL (1m):** TP=70 / SL=50, separate `_check_tp_sl_minute` code path w/ no entry logic.
- Symmetric SHORT MACD falling-edge via `_macd_rising_edge(-macd)`.

`_snapshot_ind` uses fixed 8-key `dict.fromkeys(...)` shape so frontend `TradeIndicators` type contract stable.

### Signal payload

`Signal.payload` carries `entry_ind` (open) + `exit_ind` (close): 8-key snapshots `{k, d, macd, signal, hist, plus_di, minus_di, adx}` rounded to 2 decimals, NaN → None. `PositionTracker._open_trade` writes `entry_ind` into `Trade.payload`; `_close` does:

```sql
UPDATE … SET payload = COALESCE(payload, '{}'::jsonb) || jsonb_build_object('exit_ind', :exit_ind)
```

so existing `entry_ind` preserved.

**Fill convention deviation:** signals fire on bar close (framework limit). Spec calls for next-bar-open fill — documented in module docstring as deferred.

## Tests

`backend/tests/` covers everything — indicator math, notifier hub fan-out + per-channel failure isolation + channel filter, FinMind adapter (dedupe, invalid-row tolerance, sub-floor rejection, front-month picker w/ R1 alias preference + volume fallback + contract_date tiebreak + NaN-safe coercion, day/night `_market_open`), position tracker (open/close/flip/idempotency/rehydrate), trades API `compute_stats` (extracted as pure function so tests skip DB), insights cache (TTL + LRU + key sensitivity), insights service (system-prompt persona, `cache_control` marker, JSON-encoded payload escapes prompt-injection string, compare mode `compare_a`/`compare_b` payload + `COMPARE_SYSTEM_TAIL` tail block + original `SYSTEM_PROMPT` byte-unchanged), backfill (trading-day filter, FinMind client parse + quota path, `_missing_days` threshold + today-skip, range/recent flows, spread/floor/dominant-contract filters), `/bars` cutoff exclusion, `IngestRunner` watchdog tick + tombstone double-emit guard + grace window, `trade_strat_v1` + `trade_strat_v2` (entry/exit/TP/SL, dump_state shape, daily confidence count, rising-edge entry, no-repeat-without-reset), `/strategies/{name}/state` route, backtest engine (pair logic for long+exit / reverse / same-direction / orphan exit, stats math, equity curve cumulative, end-to-end smoke w/ stub strategy + patched `load_bars`, state isolation, empty-history + 404, LRU hit/miss w/ module_mtime invalidation), `GET /backtest/run` shape parity w/ `POST`, `bars_2m` + `bars_3m` + `bars_10m` route presence, `GET /signals` filter by `strategy`/`since`/`limit`, `GET /alerts/stats` aggregation, `POST /admin/test-webhook` 503-when-unset / 200-success / channel-routing, lens URL/localStorage round-trip. **205 tests as of V5.3.** Confirm via `cd backend && uv run pytest -q | tail -1`.

**No tests require live DB.** Mock at `session_scope()` boundary or extract SQL-touching logic into pure function (e.g. `compute_stats` — SQL part in `_query_trades`, math separate).

## Backlog (deferred)

- CORS still wide open.
- Mutating endpoints unauthenticated: `/strategies/*`, `/insights/strategy`, `/admin/backfill`, `/admin/test-webhook`, `/backtest/run`.
- No global Anthropic spend cap.
- Reverse-proxy IP gap on rate limiter (collapses to single bucket if `X-Forwarded-For` stripped).
- No TW holiday calendar (backfill iterates Mon-Fri incl holidays — wasted API calls).
- `/admin/backfill` synchronous (multi-month windows block).
- No per-trade fees / slippage / position sizing in backtest engine.
- Backtest fills at signal-bar close (no `next_bar_open` mode).
- No auth / multi-user.
- `_STATE` swap convention module-introspection-based; brittle to non-`_STATE` naming.
- `wipe_and_rebackfill.py` no env guard.
- Front-month picker assumes FinMind keeps `R1` rolling-alias suffix (log when fallback triggers).
- Dominant-contract backfill filter falls through to "keep everything" when all rows have empty `contract_date` (logs warning, doesn't fail).

## Operational gotcha — adding env keys to `.env`

`docker compose restart backend` does NOT re-read `env_file`. Container keeps env vars baked in at creation. To pick up newly-added env key (e.g. `DISCORD_WEBHOOK_URL`):

```sh
docker compose up -d --force-recreate backend
```

Verify env var landed in container:

```sh
docker compose exec backend env | grep DISCORD_WEBHOOK_URL
curl -s http://127.0.0.1:8000/status | python3 -m json.tool | grep discord
# notifiers.discord should be true after the recreate
```

`_notifier_presence` helper in `app/api/routes/status.py:57-69` reads `settings.discord_webhook_url` via `@lru_cache`-cached `get_settings()`. Frontend `AlertLog.tsx` only renders `測試發送` button when `/status` reports channel as configured. Missing env var after restart shows up as missing test button.

## Verification quick-reference

Backend health:

```sh
curl -s http://127.0.0.1:8000/status | python3 -m json.tool
# all of: ingest_running, strategy_loop_running, position_tracker_running, db_ok, ok → true
# notifiers.discord → true once .env's DISCORD_WEBHOOK_URL is loaded into the container
```

Confirm paper trading live:

```sh
curl -s 'http://127.0.0.1:8000/strategies' | python3 -c "import json,sys; [print(f\"{s['name']:20s} enabled={s['enabled']}\") for s in json.load(sys.stdin)]"
curl -s 'http://127.0.0.1:8000/trades?limit=5' | python3 -m json.tool | head -50
curl -s 'http://127.0.0.1:8000/trades/stats?strategy=trade_strat_v1' | python3 -m json.tool
```

Pre-V5 trades carry empty `payload: {}` — predate indicator-snapshot threading + won't be backfilled. New trades carry full `entry_ind` / `exit_ind`.
