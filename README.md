# TAIEX MXF Dashboard

Real-time TAIEX futures dashboard and strategy runner. Live ticks come from
the Shioaji (SinoPac) API on the rolling near-month contract `TXFR1`
(displayed as **MXF**; strategies operate in index points, agnostic to
contract size). Features:

- togglable indicators (MACD, DMI, KD, RSI, MA) on true chart panes,
- multi-resolution bars (1m/2m/3m/5m/10m/15m/30m/1h/4h/12h/1d/1w/1mo) built
  as TimescaleDB continuous aggregates over a single `ticks` hypertable,
- a plug-in strategy framework (bar-close or tick-driven) with per-strategy
  params managed over REST,
- a backtest engine replaying registered strategies over historical bars
  with intra-bar worst-case stop fills,
- position tracking (entry/exit pairing into trades with PnL) and fan-out
  alerting to Discord / n8n / in-app WebSocket,
- a feed-health watchdog that detects silent tick starvation during market
  hours and forces a full re-login instead of trusting the SDK reconnect.

```
[Shioaji TXFR1 push] ──► [adapter] ──► [ingest loop] ──► TimescaleDB (ticks)
                                            │                  │
                        ┌───────────────────┼──────────┐   (continuous
                        ▼                   ▼          ▼    aggregates)
                   [indicators]      [strategy loop] [WS broadcast]
                                            │
                                            ▼
                              [notifier hub: discord / n8n / in-app]
                                            │
                                            ▼
                              [position tracker ──► trades + stats]
```

## Run (Docker, recommended)

```sh
cp .env.example .env        # fill in SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY,
                            # optional DISCORD_WEBHOOK_URL etc.
docker compose up --build   # db + backend + frontend, hot-reload all three
```

Browser → `http://localhost:3000`. Backend on `:8000`, TimescaleDB on
`:5432`. Source is bind-mounted; both servers hot-reload. Note
`docker compose restart` does **not** re-read `.env` — after adding env
keys use `docker compose up -d --force-recreate backend`.

## Run (host, no Docker)

```sh
docker compose up -d db
cd backend && uv sync --extra dev
uv run alembic upgrade head   # schema + continuous aggregates
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# second terminal
cd frontend && npm install && npm run dev   # http://127.0.0.1:3000
```

Both processes bind to `127.0.0.1` only — nothing on the LAN can reach them
directly. Browser → Next dev server (3000) → rewrites `/api/*` and `/ws/*`
to FastAPI on 127.0.0.1:8000, so one origin covers REST + WebSocket.

## Strategies

Trading strategies plug into the framework below; the implementations
running in production are kept private and are not part of this repo
(`always_long` ships as a minimal example). Per-strategy `enabled` /
`params` / `channels` live in the `strategy_config` table and are managed
via `GET/PATCH /strategies`. Each closed trade carries an indicator
snapshot at entry and exit so the analysis log shows the full conditions
side-by-side.

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

Strategies may also opt into tick-driven dispatch (`tick_resolutions`) and
cross-resolution auxiliary indicators (`aux_indicator_specs`). Restart the
backend to activate; the dashboard's strategy panel discovers it
automatically.

## Backtesting

`POST /backtest/run` replays any registered strategy over closed historical
bars and returns signals, paired trades, stats (PF, drawdown, win rate) and
an equity curve. The analysis page renders it, including side-by-side
strategy comparison (`/analysis?compare=1`).

## Tests

```sh
cd backend && uv run pytest -q
cd frontend && npx tsc --noEmit
```

## Notes

- **Shioaji limits:** SinoPac caps 5 concurrent connections per person ID
  and 1000 logins/day. The client is a single-process singleton —
  single-worker uvicorn is required.
- **Sessions:** TAIFEX day 08:45–13:45, night 15:00–05:00 Asia/Taipei. The
  feed emits nothing outside sessions; strategy entry windows are enforced
  in `app.strategies.base`.
- **Data quality:** price floor/ceiling guards at the ingest boundary reject
  zero/garbage ticks; historical backfill sanity-checks session windows.
- Continuous aggregates exist for 1m–1d; 1w and 1mo are plain views over
  the 1d aggregate. The refresh policy runs every 30 s, so the most recent
  finalised bar lags the present by ≤ 1m; the in-progress bar streams over
  WebSocket.
- Strategies expose an optional `display_name` for the UI while the
  canonical `name` remains the DB key for `trades.strategy`,
  `signals.strategy`, and `strategy_config.name`.
