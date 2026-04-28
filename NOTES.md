# Operator notes — TAIEX MXF dashboard

Personal runbook. Three local processes: TimescaleDB (Docker), FastAPI
backend, Next.js frontend. All bound to `127.0.0.1` only. Sharing access
to other devices goes through Tailscale.

---

## 0. One-time setup

```sh
# clone + env
git clone <this repo>
cd TAIEX
cp .env.example .env       # fill in FINMIND_TOKEN, DISCORD_WEBHOOK_URL, N8N_WEBHOOK_URL

# backend deps
cd backend && uv sync --extra dev

# DB up + schema
cd ..
docker compose up -d db
cd backend && uv run alembic upgrade head

# frontend deps
cd ../frontend && npm install
```

`.env` is gitignored. Never commit it. If you rotate any secret, just
edit `.env` and restart the backend.

---

## 1. Daily start

Three terminals, in this order. Each one waits for the previous to be
ready before the next is useful.

### Terminal 1 — database

```sh
cd /Users/raccoon/Desktop/TAIEX
docker compose up -d db
docker compose ps                 # wait until taiex-timescale shows "healthy"
```

If Docker Desktop isn't running yet, open it from Applications first.

### Terminal 2 — backend (FastAPI + ingest loop)

```sh
cd /Users/raccoon/Desktop/TAIEX/backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Wait for `Application startup complete`. The ingest loop will:

- Backfill the last 2 days of TAIEX 5-sec ticks from FinMind on startup.
- During TW market hours (08:45–13:45 Asia/Taipei) poll every 5 s.
- Outside market hours, sleep and emit a heartbeat.

### Terminal 3 — frontend (Next.js)

```sh
cd /Users/raccoon/Desktop/TAIEX/frontend
npm run dev
```

Open **http://127.0.0.1:3000** (NOT `localhost`, NOT `192.168.x.x`).

---

## 2. Daily stop

```sh
# in each running terminal
Ctrl+C

# in any terminal
cd /Users/raccoon/Desktop/TAIEX
docker compose stop                # data persists in the taiex-pg volume
```

To wipe the DB completely (resets ticks/signals/alerts):

```sh
docker compose down -v
```

---

## 3. Sharing access via Tailscale

Goal: only people you invite can reach the dashboard. Servers stay
bound to `127.0.0.1`; Tailscale exposes port 3000 (Next dev) onto the
private mesh.

### One-time host install

```sh
brew install --cask tailscale       # opens the Tailscale app
# sign in (Google / GitHub / Microsoft) — creates your tailnet
tailscale up
tailscale status                    # confirm host is "active"
```

### Expose the dashboard onto the tailnet

```sh
# share the Next dev port over HTTPS within the tailnet
tailscale serve --bg --https=443 http://127.0.0.1:3000

# print the URL
tailscale serve status
```

The URL looks like `https://your-mac.tailxxxx.ts.net/`. Bookmark it.

### Inviting someone

1. They install Tailscale on their device (iOS, Android, macOS, Windows, Linux).
2. You go to <https://login.tailscale.com/admin/users> → **Invite**, type their email.
3. They accept the email invite, sign in to Tailscale.
4. They open the URL from `tailscale serve status`.

### Stop sharing

```sh
tailscale serve --https=443 off
```

Or just `tailscale down` to leave the tailnet entirely.

### Notes

- Both `next dev` and `uvicorn` bind `127.0.0.1` on purpose. Anyone on
  the same Wi-Fi who tries `http://192.168.x.x:3000` gets connection
  refused. Tailscale is the only public entry point.
- The Next dev server proxies REST (`/api/*`) and WebSocket (`/ws/*`) to
  FastAPI on `127.0.0.1:8000`, so only port 3000 needs to go through
  Tailscale Serve.
- If the host Mac sleeps, the dashboard goes down for everyone on the
  tailnet until you wake it. There is no 24/7 ingest. Consider a small
  always-on host (VPS, Raspberry Pi) when you outgrow this.

---

## 4. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Backend: `ConnectionRefusedError [Errno 61]` | DB container not up | `docker compose up -d db`, wait for healthy |
| Backend: `relation "ticks" does not exist` | Schema not migrated | `cd backend && uv run alembic upgrade head` |
| Backend: stuck on FinMind 400 | Token expired / quota | Check `FINMIND_TOKEN` in `.env`, regenerate at finmindtrade.com |
| Frontend: chart empty, WS reconnecting | Backend down, or DB has no bars yet | Confirm backend running; wait for first 1-min bar to close |
| Frontend: zh-TW characters render as boxes | Google Fonts blocked | Allow `fonts.googleapis.com` / `fonts.gstatic.com` in any blocker |
| Frontend: `500 Internal Server Error` from `/strategies` | Schema not migrated, or DB down | Run alembic, restart backend |
| `npm run dev` exposes on LAN | Old script | Pull latest; `package.json` `dev` script must include `-H 127.0.0.1` |
| Tailscale URL fails for invited user | They didn't accept invite, or aren't logged in to Tailscale | Re-send invite from admin console |

---

## 5. Adding a strategy

Drop a new file under `backend/app/strategies/examples/` (or ship as a
pip package exposing entry point group `taiex.strategies`):

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
        macd = ev.indicators["macd"]
        if macd.iloc[-1]["hist"] > 0 and macd.iloc[-2]["hist"] <= 0:
            return Signal(
                ts=ev.bucket, symbol=ev.symbol, resolution=ev.resolution,
                strategy=self.name, side="LONG",
                price=float(ev.bars["close"].iloc[-1]),
                reason="MACD bullish cross",
            )
```

Restart backend, refresh dashboard. The strategy panel auto-discovers it.
Toggle it On — signals start flowing to Discord + n8n + the in-app log.

---

## 6. Tests

```sh
cd backend && uv run pytest -q
```

Currently 13 tests covering indicator math, FinMind dedupe, notifier
hub fan-out + failure isolation. No DB required for these.
