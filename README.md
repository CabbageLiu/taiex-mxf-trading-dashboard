# TAIEX MXF Dashboard

Real-time TAIEX cash-index dashboard (proxy for 小台指期 MXF) with togglable
indicators (MACD, DMI, KD, RSI, MA), multi-resolution bars
(1m/2m/3m/5m/10m/15m/30m/1h/4h/12h/1d/1w/1mo), a plug-in strategy framework, and
fan-out alerting to Discord + n8n.

Two example strategies ship in-tree (`trade_strat_v1` / `trade_strat_v2`)
with Traditional-Chinese display names (`30分鐘線策略` / `5分鐘策略`)
backed by the canonical DB key for cache stability. Each closed trade
carries a KD / MACD / DMI snapshot at entry and exit so the analysis
log shows the full conditions side-by-side.

```
[FinMind 5-sec TAIEX] ──► [adapter] ──► [ingest loop] ──► TimescaleDB
                                              │
                          ┌───────────────────┼─────────────────────┐
                          ▼                   ▼                     ▼
                     [indicators]       [strategy runner]      [WS broadcast]
                                              │
                                              ▼
                                     [notifier hub: discord / n8n / in-app]
```

## One-time setup

```sh
cp .env.example .env          # fill in FINMIND_TOKEN, DISCORD_WEBHOOK_URL, N8N_WEBHOOK_URL
docker compose up -d db       # TimescaleDB on :5432
cd backend && uv sync --extra dev
uv run alembic upgrade head   # create schema + continuous aggregates
cd ../frontend && npm install
```

## Run

```sh
# terminal 1 — backend (bound to localhost only)
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# terminal 2 — frontend (also bound to 127.0.0.1 by the npm script)
cd frontend && npm run dev   # http://127.0.0.1:3000
```

Both processes bind to `127.0.0.1` only — nothing on the LAN can reach
them directly. Browser → Next dev server (3000) → rewrites `/api/*` and
`/ws/*` to FastAPI on 127.0.0.1:8000.

## Sharing access (Tailscale)

To let specific people in (and only them), put the host on a tailnet and
expose the dashboard via `tailscale serve`:

```sh
brew install --cask tailscale       # host install + sign in
tailscale up

# expose Next dev (which already proxies REST + WS to FastAPI)
tailscale serve --bg --https=443 http://127.0.0.1:3000
tailscale serve status              # prints the tailnet HTTPS URL
```

Each allowed user installs Tailscale on their device and is invited to
your tailnet (Tailscale admin → Users → Invite). They visit the printed
`https://<host>.<tailnet>.ts.net/` URL — anyone not on the tailnet sees
nothing because the dev servers are bound to localhost.

To stop sharing: `tailscale serve --https=443 off`.

The ingest loop polls FinMind every 5 s during TW market hours
(08:45–13:45 Asia/Taipei). Outside hours it backfills the most recent
trading day on startup so the chart isn't empty.

## Adding a strategy

Drop a Python module under `backend/app/strategies/examples/` (or ship it
as a separate pip package exposing the `taiex.strategies` entry point):

```python
from pydantic import BaseModel
from app.strategies.base import BarEvent, Signal, Strategy
from app.strategies.registry import register_strategy

class Params(BaseModel):
    fast: int = 12
    slow: int = 26

@register_strategy
class MyStrategy(Strategy):
    name = "my_strategy"
    resolutions = ["5m", "15m"]
    params_schema = Params
    indicator_specs = {
        "macd": {"kind": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
    }

    def on_bar(self, ev: BarEvent) -> Signal | None:
        macd = ev.indicators["macd"].iloc[-1]
        if macd["hist"] > 0 and ev.indicators["macd"].iloc[-2]["hist"] <= 0:
            return Signal(
                ts=ev.bucket, symbol=ev.symbol, resolution=ev.resolution,
                strategy=self.name, side="LONG", price=float(ev.bars["close"].iloc[-1]),
                reason="MACD bullish cross",
            )
```

Restart the backend (or `POST /strategies/{name}/enable`) to activate.
The dashboard's strategy panel discovers the new strategy automatically.

## Tests

```sh
cd backend && uv run pytest -q
```

## Notes

- The on-chart symbol label is **MXF** but the live data feed is the TAIEX
  cash index from FinMind's free `TaiwanVariousIndicators5Seconds` dataset.
  Swap to a real MXF feed (Shioaji, paid FinMind) by writing a new
  `MarketDataAdapter` in `backend/app/adapters/`; nothing else changes.
- Continuous aggregates exist for 1m–1d (including the 2m / 3m / 10m
  buckets the example strategies use). 1w and 1mo are plain views over
  the 1d aggregate to keep the migration simple.
- `1m` continuous aggregate refresh policy runs every 30 s with a 1m
  end-offset, so the most recent finalised bar lags the present by ≤ 1m.
- Strategies expose an optional `display_name: ClassVar[str]` so the UI
  can render a human-friendly label while the canonical `name` remains
  the DB key for `trades.strategy`, `signals.strategy`, and
  `strategy_config.name`. UI components fall back to `name` whenever
  `display_name` is unset.
