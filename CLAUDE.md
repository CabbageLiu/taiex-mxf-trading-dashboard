# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### One-command dev stack (recommended)

```sh
docker compose up           # db + backend + frontend, hot-reload all three
docker compose up --build   # first run, or after dependency changes
docker compose down         # clean stop (data preserved)
docker compose down -v      # also nuke ticks/signals/alerts
```

Browser → `http://localhost:3000`. Backend on `:8000`. DB on `:5432`. Source is bind-mounted; both servers hot-reload on file edits.

### Host workflow (no Docker)

Use this when you want pytest, ruff, or alembic revisions outside containers.

#### Local infrastructure

```sh
docker compose up -d db          # TimescaleDB on 127.0.0.1:5432, volume taiex-pg
docker compose ps                # wait for STATUS=healthy
docker compose stop              # keep data
docker compose down -v           # nuke ticks/signals/alerts
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
MarketDataAdapter (adapters/finmind_taiex.py)   ← swap point for shioaji/MXF feed
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
                                          └─► InAppNotifier ─► WS
                       (every attempt logged to alerts table)
```

The **ingest runner is the single source of bar events**. Strategies and the WebSocket both subscribe to its in-process `asyncio.Queue` fan-out via `IngestRunner.subscribe(resolution)`. There is no separate scheduler — bar timing is derived from tick `ts` rounded into resolution buckets in `ingest/runner.py:_bucket_start`.

### TimescaleDB schema

`ticks` is the only hypertable that holds raw data. Bars are derived:

- **1m, 5m, 15m, 30m, 1h, 4h, 12h, 1d** — Timescale **continuous aggregates** with a `add_continuous_aggregate_policy` running every 30 s.
- **1w, 1mo** — plain views built on top of `bars_1d` (continuous-aggregate-of-continuous-aggregate has restrictions, so the long buckets fall back to views).

The `/bars` endpoint in `app/api/routes/bars.py` reads whichever view matches the requested resolution. **There is no `bars` table.** Adding a new resolution means: add to `RESOLUTIONS` in `ingest/runner.py`, add to `VALID_RES` in `api/routes/bars.py`, and add the matching view to migration `0001_init`.

### Indicator service

Five hand-rolled indicators in `app/indicators/`: MA, MACD, RSI (Wilder), KD (TW-style 3-EMA smoothing), DMI (Wilder ADX). **No `pandas-ta` dependency** — the unmaintained package breaks on numpy 2 / pandas 3. `IndicatorCache` in `service.py` keys on `(symbol, resolution, kind, frozen-params)` and invalidates only when the latest bar timestamp moves, so the same series is reused across REST + strategy evaluations within one bar.

### Strategy plug-in framework

`Strategy` ABC + `BarEvent`/`Signal` dataclasses live in `app/strategies/base.py`. The registry in `app/strategies/registry.py` does two things:

1. **In-repo:** `discover()` walks `app/strategies/examples/` and imports every module, picking up anything decorated with `@register_strategy`.
2. **External:** also loads any `taiex.strategies` Python entry-point group, so a strategy can ship as a separately-installed pip package.

A strategy declares `resolutions: list[str]` and an optional `indicator_specs: dict[label, {kind, params}]`; the runner precomputes those indicators and passes them in via `BarEvent.indicators`. Per-strategy `enabled`, `params`, and `channels` live in the `strategy_config` table and are managed via `/strategies` REST endpoints.

### Notifier hub

`NotifierHub.dispatch()` runs all configured notifiers in `asyncio.gather` and writes one `alerts` row per channel attempt. Failures are caught per-notifier; one bad webhook never starves the others. The `InAppNotifier` is in-process and publishes onto a queue that the WebSocket endpoint subscribes to — operators see signals in the dashboard even when both webhooks are misconfigured.

### Configuration

`app/config.py` uses `pydantic-settings` with `env_file=("../.env", ".env")`, so the same `.env` at repo root works whether commands run from `backend/` or the project root. Settings are cached via `@lru_cache` on `get_settings`. The display symbol (`MXF`) is decoupled from the source (`TAIEX`) — the adapter labels every tick with `symbol_display`, so swapping the data feed changes only the adapter file.

### Frontend

Next.js 15 App Router. Single page, single locale (`zh-Hant-TW`); `lib/i18n.ts` is a tiny dict + `t()` helper. **Indicator names stay English** by design — never wrapped in `t()`.

The chart (`components/Chart.tsx`) uses **TradingView Lightweight Charts v5**. Each indicator pane is a separate `priceScaleId` (`macd` / `rsi` / `kd` / `dmi`) on the same chart, with `scaleMargins: {top: 0.7, bottom: 0}` to stack them under the price pane. **Candle convention is TW: red = up 漲 (#c0392b), green = down 跌 (#3a7d4f)** — opposite of the US convention. Histograms and DMI lines follow the same colour grammar.

Live updates merge into the chart in `Chart.tsx`: every `bar_update` WS message either appends a new bar or extends the in-progress one (high/low/close mutate, open is sticky). Bar history is fetched once via `/api/bars` on resolution change; ongoing bars come from the WS only.

`lib/ws.ts` derives the WebSocket URL from `window.location` (not from an env var) so the same code works on `127.0.0.1:3000` and on a Tailscale Serve hostname without configuration. The `next.config.mjs` rewrite for `/ws/:path*` is what makes that work.

### Tests

`backend/tests/` covers indicator math (against straight-uptrend fixtures), notifier hub fan-out + per-channel failure isolation + channel filter, and FinMind adapter dedupe + invalid-row tolerance. **None of the tests require a live database.** When adding a feature that needs DB I/O, mock at the `session_scope()` boundary or extract the SQL-touching logic into a function the test can patch.
