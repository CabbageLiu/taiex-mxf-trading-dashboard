# Operator notes — TAIEX MXF dashboard

Personal runbook + user guide. Whole stack (TimescaleDB + FastAPI backend +
Next.js frontend) runs in Docker via one command. Ports bind to `127.0.0.1`
only. Sharing access to other devices goes through Tailscale.

The dashboard has two pages, both 繁體中文:

- **`/trading`** — live candle chart of the configured futures contract
  (default `MXF`), TopBar with status pill / resolution selector / strategy
  selector / indicator toggles (MACD / KD / DMI / RSI / MA), crosshair
  tooltip showing OHLC + every enabled indicator value at the hovered time.
  Right-rail alert log streams strategy signals as they fire.
- **`/analysis`** — strategy review. KPI strip (勝率, 交易筆數, 累積損益,
  最大回撤), filterable trade list (date range + 全部 / 獲利 / 虧損),
  pattern-analysis panel, and a `生成洞察` button that calls Claude Sonnet
  4.6 for 繁體中文 bullet-point coaching against the current filter.
  Trades table columns (V5): `編號 / 日期 / 方向 / 策略 / 進場 / 開倉指標
  / 出場 / 出場指標 / 持倉時長 / 損益`. The two indicator columns render
  KD / MACD / +DI / -DI snapshots captured at entry and exit by the
  position tracker (compact form `K54 D51 / MACD+9 / +DI33 -DI19`). Older
  trades pre-V5 render `—` in both columns. Hovering the strategy cell
  exposes the canonical `name` via `title=`.

---

## 0. One-time setup

```sh
git clone <this repo>
cd TAIEX
cp .env.example .env       # fill in FINMIND_TOKEN, DISCORD_WEBHOOK_URL,
                            # N8N_WEBHOOK_URL, TAIEX_ANTHROPIC_API_KEY (optional)
docker compose up --build  # first build pulls images + installs deps
```

`.env` is gitignored. Never commit it. Rotate any secret → edit `.env`,
restart with `docker compose restart backend`.

For the host fallback workflow (running pytest / ruff / alembic outside
containers), also do:

```sh
cd backend && uv sync --extra dev
cd ../frontend && npm install
```

### Required env keys

| Key                       | Default                  | What it does                                                    |
|---------------------------|--------------------------|------------------------------------------------------------------|
| `FINMIND_TOKEN`           | —                        | Sponsor-tier required for the `taiwan_futures_snapshot` endpoint |
| `DATABASE_URL`            | `postgresql+asyncpg://…` | Override only if the DB is not the bundled docker-compose service |
| `DISCORD_WEBHOOK_URL`     | —                        | Empty disables Discord notifier                                  |
| `N8N_WEBHOOK_URL`         | —                        | Empty disables n8n notifier                                      |
| `ALERT_SECRET`            | —                        | Sent as `X-Alert-Secret` header on n8n webhook calls             |
| `SYMBOL_SOURCE`           | `TXF`                    | Contract pulled from FinMind sponsor `taiwan_futures_snapshot`. Supported `data_id` values are **`TXF` / `TMF` / `CDF`** — `MXF` returns 0 rows on this endpoint and freezes the feed. |
| `SYMBOL_DISPLAY`          | `MXF`                    | Label shown in the UI and stamped on every tick. Decoupled from `SYMBOL_SOURCE` so the chart can read "MXF" while the data comes from TXF (both track the same TAIEX index). |
| `POLL_INTERVAL_SEC`       | `5`                      | FinMind poll cadence                                             |
| `TIMEZONE`                | `Asia/Taipei`            | Used everywhere — never set anything else                        |
| `MARKET_OPEN/CLOSE`       | `08:45` / `13:45`        | Adapter sleeps outside this window                               |

V2 additions (all optional):

| Key                              | Default              | What it does                                              |
|----------------------------------|----------------------|------------------------------------------------------------|
| `TAIEX_ANTHROPIC_API_KEY`        | unset                | Without this, `/analysis` AI panel returns 503 (UI degrades cleanly). Prefixed to avoid colliding with Claude Code's `ANTHROPIC_API_KEY`. |
| `ANTHROPIC_MODEL`                | `claude-sonnet-4-6`  | Override only when migrating models                       |
| `INSIGHTS_CACHE_TTL_SECONDS`     | `1800`               | How long an AI insight stays cached before regeneration   |
| `INSIGHTS_CACHE_MAX_ENTRIES`     | `256`                | Bounded LRU; restart drops the cache (no Redis dep)       |

---

## 1. Daily start

One command, one terminal:

```sh
cd /Users/raccoon/Desktop/TAIEX
docker compose up
```

Open **http://localhost:3000** → redirects to `/trading`.

What happens:

- `db` (TimescaleDB) starts, waits until healthy.
- `backend` starts, runs `alembic upgrade head` (idempotent), then `uvicorn`
  on port 8000. Healthcheck polls `/health`.
- `frontend` waits for backend healthy, then runs `next dev` on port 3000.
- Logs from all three interleave. Ctrl-C stops everything.

The status pill in the top-right of every page shows live backend health:

| Dot color | Meaning                                       |
|-----------|-----------------------------------------------|
| Green     | `/status.ok = true` — ingest fresh, DB reachable |
| Amber     | `ingest_lag_seconds > 30` — ticks stale         |
| Red       | Fetch failed or `ok = false`                  |

Hover the pill for the full breakdown (last tick, lag seconds, DB ok,
notifier presence per channel).

During TW market hours (08:45–13:45 Asia/Taipei) the backend ingest loop
polls FinMind every 5 s. Outside market hours it sleeps and emits a
heartbeat.

> **Note:** the FinMind backfill is gone (the new sponsor
> `taiwan_futures_snapshot` endpoint is real-time only). The first time
> you start during market hours, the chart is empty until the first bar
> closes; subsequent days resume from whatever ticks were captured.

### Adding or changing keys in `.env`

`docker compose restart backend` does NOT re-read `env_file`. The container keeps the env vars that were baked in at creation. Any new key (e.g. `DISCORD_WEBHOOK_URL`) added to `.env` requires a recreate:

```sh
docker compose up -d --force-recreate backend
```

Verify the var actually landed inside the container before chasing other bugs:

```sh
docker compose exec backend env | grep DISCORD_WEBHOOK_URL
curl -s http://127.0.0.1:8000/status | python3 -m json.tool | grep discord
# both should show the configured URL / true
```

A missing env var after a `restart` shows up as a missing 測試發送 button on the right rail — the frontend reads `/status notifiers.discord` and only renders the button when the channel is configured. The `_notifier_presence` helper in `backend/app/api/routes/status.py` reads `settings.discord_webhook_url` via `get_settings()` (which is `@lru_cache`-cached at module level, so the same caveat applies to non-Docker host workflows: kill + relaunch uvicorn after editing `.env`).

### When to use `--build`

| Change | Command |
|---|---|
| Edited Python / TypeScript source | `docker compose up` (hot-reload picks it up) |
| Edited `pyproject.toml` (added a Python dep) | `docker compose up --build` |
| Edited `package.json` (added an npm dep) | `docker compose up --build` |
| First time ever, or after `down -v` | `docker compose up --build` |

---

## 2. Daily stop

```sh
# in the running terminal
Ctrl+C

# or, if running detached (`docker compose up -d`)
docker compose down              # stops containers, KEEPS data in taiex-pg volume
```

To wipe the DB completely (resets ticks/signals/alerts/trades):

```sh
docker compose down -v
```

⚠ V2: `docker compose down -v` also drops the `trades` table. Realised PnL
history goes with it. Take a `pg_dump` first if you care about it.

---

## 3. Editing while it runs

Both servers hot-reload via bind mounts:

- Edit `backend/app/**.py` → uvicorn reloads (~1 s).
- Edit `frontend/**.tsx` / `**.ts` → Next HMR refreshes the browser.

No restart needed unless you change `pyproject.toml` / `package.json` /
`docker-compose.yml` / Dockerfiles → then `docker compose up --build`.

---

## 4. Host workflow (no Docker)

Use this for one-shot dev tools that don't need the full stack:

```sh
cd backend
uv run pytest -q                              # 44 tests as of V2, no DB needed
uv run ruff check .                           # lint
uv run alembic revision -m "msg" --autogenerate   # author a new migration

cd ../frontend
npx tsc --noEmit                              # typecheck only
npm run build                                 # production build
```

You can also run the backend on the host while the DB stays in Docker:

```sh
docker compose up -d db
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

---

## 5. Sharing access via Tailscale

Goal: only people you invite can reach the dashboard. Containers
publish on `127.0.0.1`; Tailscale exposes port 3000 onto the private
mesh.

### One-time host install

```sh
brew install --cask tailscale
tailscale up
tailscale status
```

### Expose the dashboard onto the tailnet

```sh
tailscale serve --bg --https=443 http://127.0.0.1:3000
tailscale serve status                          # prints the URL
```

URL looks like `https://your-mac.tailxxxx.ts.net/`. Bookmark it.

### Inviting someone

1. They install Tailscale (iOS / Android / macOS / Windows / Linux).
2. <https://login.tailscale.com/admin/users> → **Invite**, type their email.
3. They accept, sign in to Tailscale.
4. They open the URL from `tailscale serve status`.

### Stop sharing

```sh
tailscale serve --https=443 off
# or
tailscale down
```

### Notes

- All published ports use `127.0.0.1:` prefix (`5432`, `8000`, `3000`).
  Anyone on the same Wi-Fi trying `http://192.168.x.x:3000` gets
  connection refused. Tailscale is the only public entry point.
- Next dev proxies REST (`/api/*`) and WebSocket (`/ws/*`) to the
  backend container internally, so only port 3000 needs Tailscale Serve.
- If the host Mac sleeps, the dashboard goes down for everyone on the
  tailnet. No 24/7 ingest. Consider an always-on host (VPS, Pi) when
  you outgrow this.
- ⚠ V2: CORS is wide open and mutating endpoints are unauthenticated.
  Tailscale-only deploys are fine; public exposure is **not** safe yet.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `docker compose up` hangs at "backend Starting" | First-run image pulls | Wait 1–2 min; `docker compose logs backend` to watch progress |
| Backend exits with `ConnectionRefusedError` | DB not healthy yet | Should not happen — backend `depends_on db: service_healthy`. If it does, `docker compose logs db` |
| Frontend shows API errors right after start | Backend healthcheck not green yet | Frontend already waits for backend healthy; otherwise refresh |
| `relation "ticks" does not exist` | Migration didn't run | `docker compose logs backend` — check the alembic line. To force: `docker compose run --rm backend uv run alembic upgrade head` |
| `relation "trades" does not exist` (V2) | Migration `0002_trades` skipped | Same fix; alembic should include it automatically. Check `alembic current` matches `head` |
| FinMind 400 / 401 | Token expired or wrong tier | Check `FINMIND_TOKEN` in `.env`. Sponsor tier required for `taiwan_futures_snapshot` |
| Chart empty during market hours | First bar hasn't closed yet, or `SYMBOL_SOURCE` set to a `data_id` the FinMind sponsor endpoint doesn't serve | Wait one minute. Confirm `.env` has `SYMBOL_SOURCE=TXF` (or `TMF` / `CDF`). **`MXF` returns 0 rows from `taiwan_futures_snapshot` and freezes the feed** — keep source = TXF, change only `SYMBOL_DISPLAY` if you want a different label. |
| Price stuck at a specific time during the day | FinMind sponsor endpoint stopped returning new rows for the configured `data_id` | Hit the endpoint manually: `curl -s -H "Authorization: Bearer $FINMIND_TOKEN" "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot?data_id=TXF" \| jq '.data \| length'`. Zero rows = wrong `data_id` or sponsor tier expired. |
| Want to fill the gap from when the server was down | Backfill endpoint or wait for next boot | `curl -X POST 'http://127.0.0.1:8000/admin/backfill?start=YYYY-MM-DD&end=YYYY-MM-DD'`. See §7.8. |
| Status pill stays red | Backend down, DB unreachable, or FinMind throwing | Hover the pill for which subsystem is failing; tail `docker compose logs backend` |
| `/analysis` AI panel says `伺服器尚未設定 TAIEX_ANTHROPIC_API_KEY` | Key not set in `.env` | Add `TAIEX_ANTHROPIC_API_KEY=sk-ant-…` to `.env`, then `docker compose up -d --force-recreate backend` (restart keeps stale env). Var is prefixed so it does not clobber Claude Code's `ANTHROPIC_API_KEY`. |
| `/analysis` says rate-limited | More than 5 insights/min/(strategy, ip) | Wait the `Retry-After` seconds shown in the toast; cache hits are free, so reusing a recent filter avoids the limit |
| zh-TW characters render as boxes | Google Fonts blocked | Allow `fonts.googleapis.com` / `fonts.gstatic.com` in any blocker |
| Tailscale URL fails for invited user | They didn't accept invite, or aren't logged in | Re-send invite from admin console |
| Hot-reload not picking up changes | Editor saving outside `./backend` or `./frontend`, or filesystem watcher quirks on macOS | `docker compose restart backend` (or `frontend`) |
| Trade list empty even though strategies fired signals | Strategy emits LONG only (no EXIT/SHORT), so trades stay open and aren't counted as closed | Add an exit rule to the strategy. Open trades show as `open_count` in `/trades/stats`, not in `trade_count` |
| Want to nuke everything and start clean | — | `docker compose down -v && docker compose up --build` |

---

## 7. Adding a trading strategy

Strategies are Python plug-ins. The runner discovers them automatically on
backend startup (and on hot-reload). New strategies start **disabled** —
you must enable them from the dashboard before they fire.

### 7.1 The mental model

```
   Tick (5 s)                                                  ┌─► Discord
        │                                                      │
        ▼                                                      ├─► n8n
   IngestRunner ───► bar_close (per resolution) ───► StrategyLoop ──┘
                                                          │
                                                          ▼
                                       For each registered strategy whose
                                       `resolutions` includes this bar's
                                       resolution AND that is `enabled` in
                                       `strategy_config`:
                                          1. Build BarEvent with the last
                                             500 bars + every indicator
                                             listed in `indicator_specs`.
                                          2. Call `strategy.on_bar(ev)`.
                                          3. If it returns a Signal,
                                             persist it, fan out to the
                                             configured channels, and
                                             feed it to the position
                                             tracker (V2) which pairs it
                                             into a Trade row.
```

You write `on_bar`. Everything else is wired.

### 7.2 Minimal example — single-bar entry

Drop a new file under `backend/app/strategies/examples/` (or ship as a pip
package exposing entry-point group `taiex.strategies`):

```python
# backend/app/strategies/examples/macd_cross.py
from __future__ import annotations

from pydantic import BaseModel, Field

from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.registry import register_strategy


class Params(BaseModel):
    fast: int = Field(default=12, ge=1, le=200)
    slow: int = Field(default=26, ge=2, le=400)
    signal_period: int = Field(default=9, ge=1, le=100)


@register_strategy
class MacdCross(Strategy):
    name = "macd_cross"
    resolutions = ["5m", "15m"]
    params_schema = Params
    indicator_specs = {
        "macd": {
            "kind": "macd",
            "params": {"fast": 12, "slow": 26, "signal": 9},
        },
    }

    def on_bar(self, ev: BarEvent) -> Signal | None:
        macd = ev.indicators["macd"]
        if len(macd) < 2:
            return None
        prev_hist = macd["hist"].iloc[-2]
        curr_hist = macd["hist"].iloc[-1]
        if prev_hist <= 0 < curr_hist:
            side = "LONG"
            reason = "MACD histogram crossed up"
        elif prev_hist >= 0 > curr_hist:
            side = "EXIT"
            reason = "MACD histogram crossed down"
        else:
            return None
        return Signal(
            ts=ev.bucket,
            symbol=ev.symbol,
            resolution=ev.resolution,
            strategy=self.name,
            side=side,
            price=float(ev.bars["close"].iloc[-1]),
            reason=reason,
        )
```

Save → uvicorn auto-reloads → refresh the dashboard. The strategy now
appears in the StrategySelector dropdown.

### 7.3 Wire it up in the UI

1. Open `/trading`.
2. Click the strategy selector in the TopBar → search for `macd_cross`.
3. Toggle the row's switch on (this hits `POST /api/strategies/macd_cross/enable`
   and writes to `strategy_config`). It now fires on every matching bar close.
4. Click the gear icon → params popover. Fields are derived from
   `params_schema`. Edit values → 儲存 (`PATCH /api/strategies/macd_cross/params`).
5. Open `/analysis` and select `macd_cross` from the same selector
   (the URL `?s=macd_cross` carries the active strategy across pages).
   After a few signals fire, KPI cards + trade rows fill in.
6. Click `生成洞察` (requires `TAIEX_ANTHROPIC_API_KEY`) for AI coaching against
   the recent trade window.

### 7.4 The `Strategy` API in detail

```python
class Strategy(ABC):
    name: ClassVar[str]                                    # required — DB key
    display_name: ClassVar[str | None] = None              # optional — UI label
    resolutions: ClassVar[list[str]] = ["1m"]              # which bar sizes to fire on
    params_schema: ClassVar[type[BaseModel]] = EmptyParams # pydantic model — drives the UI
    indicator_specs: ClassVar[dict[str, dict]] = {}        # precomputed per bar close

    def __init__(self, params: BaseModel | None = None): ...

    @abstractmethod
    def on_bar(self, ev: BarEvent) -> Signal | None: ...
```

`name` is the canonical DB key — it lives in `trades.strategy`,
`signals.strategy`, `strategy_config.name`, and the `?s=` URL param.
**Never accept `display_name` as input on `/strategies/{name}/*` routes**
— it is render-only. UI components render `display_name ?? name`, with
`title={name}` on hover so power users can recover the canonical key.

#### `BarEvent`

```python
@dataclass
class BarEvent:
    symbol: str                     # e.g. "MXF"
    resolution: str                 # e.g. "5m"
    bucket: datetime                # tz-aware, the bar's bucket-start
    bars: pd.DataFrame              # last 500 closed bars
    indicators: dict[str, pd.DataFrame]   # one entry per `indicator_specs` label
```

`bars` columns: `time` (DatetimeIndex), `open`, `high`, `low`, `close`,
`tick_count`. The DataFrame is ordered ascending by time, so
`bars.iloc[-1]` is the bar that just closed.

#### `Signal`

```python
@dataclass
class Signal:
    ts: datetime
    symbol: str
    resolution: str
    strategy: str
    side: Literal["LONG", "SHORT", "EXIT", "FLAT"]
    price: float
    reason: str = ""
    payload: dict = field(default_factory=dict)
```

V5 indicator-snapshot persistence: when a strategy emits its open / close
`Signal`, it can stash an 8-key snapshot (`{k, d, macd, signal, hist,
plus_di, minus_di, adx}`) in `payload.entry_ind` / `payload.exit_ind`
respectively. The position tracker copies both into the `Trade.payload`
JSONB column at open and close (the close-side write is a
`payload || jsonb_build_object('exit_ind', …)` merge so the pre-existing
`entry_ind` is not clobbered). Snapshot values are 2-decimal-rounded
floats, NaN / missing rendered as `None`. Strategies should always emit
the full 8-key shape (use `dict.fromkeys(...)` + a NaN-safe coercion
helper) so the frontend `TradeIndicators` type stays stable.

V2 trade-pairing semantics (in `PositionTracker`):

| Current position | New `side` | Effect                                     |
|------------------|------------|---------------------------------------------|
| none             | `LONG`     | open LONG trade                             |
| none             | `SHORT`    | open SHORT trade                            |
| LONG             | `LONG`     | no-op (no stacking)                         |
| LONG             | `SHORT`    | close LONG, open SHORT (flip)               |
| LONG             | `EXIT`/`FLAT` | close LONG                              |
| SHORT            | `SHORT`    | no-op                                       |
| SHORT            | `LONG`     | close SHORT, open LONG                      |
| SHORT            | `EXIT`/`FLAT` | close SHORT                             |
| any              | replay of same signal id | no-op (idempotent)             |

Closes write `pnl_points = (exit_price - entry_price) * qty` for LONG,
mirrored for SHORT.

> Strategies that only emit `LONG` (like `always_long`) never close, so
> they never contribute to win-rate or PnL. Always pair entries with an
> exit rule unless you specifically want a watchdog signal.

#### `indicator_specs`

Map of label → `{kind, params}`. Available kinds and their output columns:

| `kind` | Params (defaults shown)                                         | DataFrame columns                |
|--------|------------------------------------------------------------------|----------------------------------|
| `ma`   | `{"period": 20, "kind": "sma"\|"ema"}`                          | `ma`                             |
| `macd` | `{"fast": 12, "slow": 26, "signal": 9}`                         | `macd`, `signal`, `hist`         |
| `rsi`  | `{"period": 14}`                                                | `rsi`                            |
| `kd`   | `{"period": 9, "k_smooth": 3, "d_smooth": 3}`                   | `k`, `d`                         |
| `dmi`  | `{"period": 14}`                                                | `plus_di`, `minus_di`, `adx`     |

Indicators are cached by `(symbol, resolution, kind, frozen-params)` and
recomputed only when the latest bar timestamp moves. Reusing the same
`indicator_specs` across multiple strategies is free; you do not pay for
each one independently.

You can ask for the same indicator at different parameters by giving each
its own label:

```python
indicator_specs = {
    "ma_fast": {"kind": "ma", "params": {"period": 9, "kind": "ema"}},
    "ma_slow": {"kind": "ma", "params": {"period": 21, "kind": "ema"}},
}
```

Then in `on_bar`: `ev.indicators["ma_fast"]["ma"].iloc[-1]`.

#### `params_schema`

Any `pydantic.BaseModel`. Field types map to UI widgets:

- `int` / `float` → number input (with `ge`/`le` enforced server-side)
- `bool` → checkbox
- `str` → text input
- `Literal[...]` → dropdown
- `default=` → pre-filled value the user can override

The runner instantiates `cls.params_schema(**(cfg["params"] or {}))` on
every bar close, so validation errors raise per bar and are logged but do
not crash the loop. Test your schema by clicking the gear icon and
saving — invalid values surface as a 422 toast.

### 7.5 Long ↔ short pair example

```python
@register_strategy
class KdSwing(Strategy):
    name = "kd_swing"
    resolutions = ["15m", "1h"]
    params_schema = Params  # define your own
    indicator_specs = {
        "kd": {"kind": "kd", "params": {"period": 9}},
    }

    def on_bar(self, ev: BarEvent) -> Signal | None:
        kd = ev.indicators["kd"]
        if len(kd) < 2:
            return None
        prev_k, prev_d = kd["k"].iloc[-2], kd["d"].iloc[-2]
        k, d = kd["k"].iloc[-1], kd["d"].iloc[-1]
        price = float(ev.bars["close"].iloc[-1])
        common = dict(
            ts=ev.bucket, symbol=ev.symbol, resolution=ev.resolution,
            strategy=self.name, price=price,
        )
        if prev_k <= prev_d and k > d and k < 30:
            return Signal(side="LONG", reason="KD golden cross in oversold", **common)
        if prev_k >= prev_d and k < d and k > 70:
            return Signal(side="SHORT", reason="KD death cross in overbought", **common)
        return None
```

The position tracker handles the LONG↔SHORT flip atomically — the LONG
closes at the SHORT signal's price, the SHORT opens immediately at the
same price.

### 7.6 Shipping a strategy as a separate package

If you'd rather keep your strategy out of this repo (e.g. private
implementation):

```toml
# pyproject.toml of your strategy package
[project]
name = "my-taiex-strategies"

[project.entry-points."taiex.strategies"]
mean_reversion = "my_taiex_strategies.mean_reversion:MeanReversion"
```

`pip install` it into the backend's environment (or add to
`backend/pyproject.toml` dependencies) and `discover()` will pick it up
on next startup. The strategy class itself still uses
`@register_strategy`.

### 7.7 What NOT to do in `on_bar`

- **No I/O.** No HTTP calls, no DB queries, no file reads. The runner
  awaits `on_bar` synchronously per resolution; a slow strategy stalls
  every other strategy on the same bar.
- **No external state.** `on_bar` may be called from a freshly
  instantiated `Strategy` object — do not assume `self.foo` set in a
  previous call survives. Either compute everything from `ev`, or stash
  state on a class-level dict keyed by `(symbol, resolution)`.
- **No sleeping or blocking.** `time.sleep`, infinite loops, etc.
- **No raising bare exceptions to signal "no trade".** Return `None`
  instead. Exceptions are caught and logged but pollute the journal.
- **No emitting `Signal` with `side` outside the enum** — anything other
  than `LONG / SHORT / EXIT / FLAT` is dropped by the position tracker.

---

## 7.8 Historical backfill (V2.5)

Live FinMind `taiwan_futures_snapshot` is real-time only — every minute the
server is down loses ticks. V2.5 wires a separate path that pulls those
ticks back from FinMind's *historical* `TaiwanFuturesTick` dataset and
inserts them into the same `ticks` table (with `source = "FINMIND_FUTURES_TICK"`
so live and backfilled rows stay distinguishable).

Two ways to trigger:

**Auto on startup.** Backend boot scans the last `BACKFILL_ON_STARTUP_DAYS`
days (default 7) for any market day with fewer than `BACKFILL_MIN_TICKS_PER_DAY`
rows (default 1000) in the `ticks` table, and refetches each. Today is
always skipped because the historical dataset updates end-of-day.

Set `BACKFILL_ON_STARTUP_DAYS=0` in `.env` to disable.

**Manual endpoint.** Use this for backtesting setup — pull a wide
historical window once, then run strategies against the resulting bars.

```sh
curl -X POST 'http://127.0.0.1:8000/admin/backfill?start=2026-04-01&end=2026-04-28'
```

Response shape:
```json
{
  "start": "2026-04-01",
  "end": "2026-04-28",
  "days": [
    {"day": "2026-04-01", "fetched": 232820, "inserted": 39789, "error": null},
    ...
  ],
  "total_inserted": 195403,
  "total_fetched": 1164100
}
```

`fetched` = rows from FinMind. `inserted` = rows actually persisted; the rest
collide on the `(ts, symbol)` primary key because FinMind tick data is
second-precision and many trades share the same second. The discrepancy
is expected and harmless — the dashboard's smallest bar is 1 minute.

Notes:

- `data_id` defaults to `MTX` (小台 — densest history, ~16k unique seconds
  per day after dedupe). Set `BACKFILL_DATA_ID=TX` in `.env` if you'd
  rather backfill the 大台 (matches live `SYMBOL_SOURCE=TXF`).
- Inserts are chunked at 5000 rows per query to stay under Postgres'
  65,535-bind-parameter limit.
- Continuous aggregates have a 30-second auto-refresh policy. After a
  large historical backfill, the bars views catch up within a minute or
  two — no manual refresh needed.
- TaiwanFuturesTick is **Backer/Sponsor tier**. Free tokens get HTTP 402
  on this dataset.
- The `/admin/backfill` endpoint is unauthenticated in V2.5 — same gap
  as the rest of the mutating endpoints. Do not expose it publicly until
  the auth layer lands.

Verify a backfill landed:

```sh
docker compose exec db psql -U taiex -d taiex -c \
  "SELECT date(ts AT TIME ZONE 'Asia/Taipei') AS d,
          source,
          count(*)
   FROM ticks
   WHERE ts >= NOW() - interval '7 days'
   GROUP BY d, source
   ORDER BY d DESC, source;"
```

Look for `FINMIND_FUTURES_TICK` rows on the days you fetched.

---

## 8. AI insights (V2)

The `/analysis` page has a `生成洞察` button that asks Sonnet 4.6 for 6
bullets of 繁體中文 coaching against the current filter (strategy + date
range + win/loss). It only fires when you click — there is no automatic
spend.

- Set `TAIEX_ANTHROPIC_API_KEY` in `.env` and `docker compose up -d --force-recreate backend` (plain `restart` does not re-read `env_file`).
- Cache: keyed on `(strategy, start, end, filter, trade_count, per-trade
  fingerprint)`. Same filter, no new closed trades → cache hit (`· 已快取`
  hint shown). New close → cache invalidates automatically.
- Rate limit: 5/min/(strategy, IP) soft cap inside the backend so a
  runaway frontend cannot blow up the bill. Cache hits do not count.
- The system prompt explicitly tells the model to treat trade-row JSON
  as data, not instructions — guards against future strategies whose
  `Signal.payload.reason` might come from external text.

---

## 9. Tests

```sh
cd backend && uv run pytest -q
```

179 tests as of V5:

- Indicator math (MA / MACD / RSI / KD / DMI) against straight-uptrend fixtures
- FinMind snapshot adapter — dedupe + invalid-row tolerance + sub-floor rejection + front-month picker + day/night `_market_open` boundaries
- Notifier hub — fan-out + per-channel failure isolation + channel filter
- V2 position tracker — open/close/flip/idempotency/rehydrate, plus V5 entry_ind/exit_ind payload pass-through (including JSONB merge that preserves entry_ind on close)
- V2 trades API — `compute_stats` win-rate / drawdown / avg-hold
- V2 insights cache — TTL + LRU + key sensitivity
- V2 insights service — system-prompt persona, cache_control marker, JSON-encoded payload (prompt-injection defence)
- V2.5 backfill — trading-day filter, FinMind quota path, `_missing_days` threshold, today-skip
- V2.6 dominant-contract filter, spread/floor guards
- V3 watchdog tick + tombstone double-emit guard, backtest engine pair logic + stats math + state isolation, `/strategies/{name}/state` route
- V4 lens URL/localStorage round-trip, `GET /backtest/run` parity with POST, backtest LRU hit/miss with module_mtime invalidation, `/insights/strategy` compare mode
- V5 strategy spec compliance: `trade_strat_v1` (30m entry, 3m -DI > 23 exit assist, MACD rising-edge, +DI > -DI, TP=220 / SL=60), `trade_strat_v2` (5m entry, 3m -DI ≥ 23 exit assist, 1m TP=70 / SL=50, symmetric SHORT MACD falling-edge, no 5m TP/SL leak); `_ind_snapshot` 8-key shape; `bars_3m` cagg + RESOLUTIONS / RESOLUTION_DELTAS / VALID_RES wiring; `display_name` ClassVar surfaced via `StrategyOut`

None require a live database. When you add a strategy, write a quick
test that feeds synthetic `BarEvent`s through `your_strategy.on_bar()`
and asserts the side/price you expect.

---

## 10. Database peek (V2)

When something looks off, connect directly:

```sh
docker compose exec db psql -U taiex -d taiex
```

Useful queries:

```sql
-- Latest tick per symbol
SELECT symbol, MAX(ts) FROM ticks GROUP BY symbol;

-- Recent signals per strategy
SELECT strategy, COUNT(*) FROM signals
WHERE ts > now() - interval '1 day'
GROUP BY strategy;

-- Open positions held by the tracker
SELECT id, strategy, symbol, side, entry_ts, entry_price
FROM trades
WHERE exit_ts IS NULL
ORDER BY entry_ts DESC;

-- Today's realised PnL per strategy
SELECT strategy,
       COUNT(*) AS trades,
       SUM(pnl_points) AS pnl
FROM trades
WHERE exit_ts IS NOT NULL
  AND exit_ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Taipei')
GROUP BY strategy;

-- Manually close a stuck open trade (rare — happens if the tracker
-- was killed mid-flip and rehydration logged a duplicate)
UPDATE trades
SET exit_ts = now() AT TIME ZONE 'Asia/Taipei',
    exit_price = <price>,
    pnl_points = CASE side
      WHEN 'LONG'  THEN (<price> - entry_price) * qty
      WHEN 'SHORT' THEN (entry_price - <price>) * qty
    END
WHERE id = <trade_id>;
```

The partial unique index `ux_trades_open_position` on (strategy, symbol)
where `exit_ts IS NULL` makes "two open trades for the same pair"
impossible at the DB layer. If you see a UniqueViolation in the backend
logs, the most likely cause is a strategy emitting LONG twice in rapid
succession after a backend restart — the second one is correctly rejected.

---

## 13. Pane height persistence (V5.1, frontend)

Chart pane heights survive indicator toggles, resolution changes, and
page reloads. Backed by a `paneHeightsRef` Map mirrored to localStorage
key `taiex.pane.heights.v1`. Snapshot points: (a) before each
`chart.removePane()` in the indicator-effect cleanup, (b) every 2
seconds via `setInterval` so user drag-resizes are captured even without
a toggle.

To reset to defaults from the browser console:

```js
localStorage.removeItem('taiex.pane.heights.v1'); location.reload();
```

Defaults: candle 320, MACD 120, RSI 100, KD 100, DMI 100 (px).

---

## 14. Discord rich embed (V5.2 / V5.3)

`backend/app/notify/discord.py` posts a Traditional-Chinese embed for
every signal. Indicator names (KD / MACD / +DI / -DI / ADX) stay
English per CLAUDE.md.

Embed shape:

- **Title**: `{display_name} → {side_TC}` — e.g. `30分鐘線策略 → 多單`.
  Side codes translated: LONG→多單, SHORT→空單, EXIT→平倉, FLAT→空手.
- **Description (OPEN)**: `進場訊號 — KD > 20 / MACD 翻正 / +DI > 21 且
  +DI > -DI` (entry-gate summary; hardcoded for v1/v2 because they
  share the spec; unknown strategies fall back to `進場條件達標`).
- **Description (CLOSE)**: `出場訊號 — {translated reason}（損益 {±value}
  點）`.
- **Fields**: 商品 / 週期 / 價格 / 時間 (Asia/Taipei `CST` suffix) /
  策略 / 開倉指標 / 出場指標 / 出場原因 / 損益 — fields are emitted
  only when relevant (no empty/null fields).
- **Footer**: `訊號 #{signal_id}` for cross-ref to `/analysis` rows.

Exit reason TC translation table (in `_EXIT_REASON_TC`):

| code | 中文 |
|---|---|
| `TP` | 達到停利目標 |
| `SL` | 觸及停損 |
| `DI_FLIP_10M` | 10 分鐘 DMI 翻轉 (-DI > +DI) |
| `MACD_DOWN_30M` | 30 分鐘 MACD 下彎 |
| `DI_FLIP` | 3 分鐘 DMI 翻轉 (legacy v1 / current v2) |
| `TRAIL` | 移動停損 |
| `DI_JUMP_1M` | 1 分鐘 -DI 跳升 (>5 點) |

Unknown reason codes pass through verbatim — add to the table when a
new strategy emits one.

Test sends fire from the right-rail `通知遞送` panel's `測試發送`
button (V4 plumbing, gated by `notifiers.discord: true` from `/status`).
The synthetic signal omits `entry_ind` so test sends are visually
distinct from real ones (no indicator block in the embed).

---

## 15. Where to look first next session

1. **Dashboard health** — `curl -s http://127.0.0.1:8000/status | python3 -m json.tool`. All five booleans (ingest, strategy_loop, position_tracker, db_ok, ok) should be true. `notifiers.discord` should be true after the user has added `DISCORD_WEBHOOK_URL` to `.env` AND recreated the backend container (`docker compose up -d --force-recreate backend`).
2. **Live paper trading** — `curl -s 'http://127.0.0.1:8000/trades?limit=5'` shows recent rows. `curl -s 'http://127.0.0.1:8000/trades/stats?strategy=trade_strat_v1'` shows aggregate.
3. **Enabled strategies** — `curl -s 'http://127.0.0.1:8000/strategies'`. `trade_strat_v1` and `trade_strat_v2` should be `enabled: true`. `always_long` should be `enabled: false` (watchdog only, never closes).
4. **Latest commits** — `git log --oneline -8`. Recent V5 series:
   - `76fd1c2` V5.3 Discord TC translation
   - `abad642` V5.2 codex-rescue nit
   - `24ef862` V5.2 rich Discord embed
   - `7e12433` V5.1 pane height + v1 exit rewrite
   - `93b48a2` V5 docs
   - `7169fe1` V5 codex-rescue blockers
   - `deedfbb` V5-C trades-table columns
   - `f5edf6f` V5-B chart marker fixes + filter pills
5. **Backlog** — §16 below. V5/V5.1/V5.2/V5.3 did not address auth, CORS, mutating-endpoint protection, Anthropic spend cap, TW holiday calendar, or backtest engine fees/slippage. The user has not asked for any of those yet.

If the user asks about a feature without context, search this NOTES.md
section index (line ~21) and CLAUDE.md before exploring code.

## 16. Deferred backlog

Cross-cutting items not yet addressed. None of these block live operation; they are visible gaps the user has not asked to close.

- CORS still wide open.
- Mutating endpoints unauthenticated: `/strategies/*`, `/insights/strategy`, `/admin/backfill`, `/admin/test-webhook`, `/backtest/run`.
- No global Anthropic spend cap.
- Reverse-proxy IP gap on rate limiter (collapses to single bucket if `X-Forwarded-For` stripped).
- No TW holiday calendar (backfill iterates Mon-Fri incl holidays — wasted API calls).
- `/admin/backfill` synchronous (multi-month windows block).
- No per-trade fees / slippage / position sizing in backtest engine.
- Backtest fills at signal-bar close (no `next_bar_open` mode). Live `strat_1k` is now tick-driven (entries + exits fire intra-bar); `strat_30k` / `strat_15k` / `trade_strat_v1` / `trade_strat_v2` still bar_close. Backtest does not replay raw ticks, so backtest results for `strat_1k` will diverge slightly from live (DI_JUMP_1M still bar-close in both modes; TP/SL/TRAIL fire at bar close in backtest vs tick price in live).
- No auth / multi-user.
- `_STATE` swap convention module-introspection-based; brittle to non-`_STATE` naming.
- `wipe_and_rebackfill.py` has no env guard — destructive, dev container only.
- Front-month picker assumes FinMind keeps `R1` rolling-alias suffix (logs when fallback triggers).
- Dominant-contract backfill filter falls through to "keep everything" when all rows have empty `contract_date` (logs warning, does not fail).

## 17. Verification quick-reference

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

Pre-V5 trades carry empty `payload: {}` — predate indicator-snapshot threading + are not backfilled. New trades carry full `entry_ind` / `exit_ind`.
