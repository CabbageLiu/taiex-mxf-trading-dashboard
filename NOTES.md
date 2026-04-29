# Operator notes — TAIEX MXF dashboard

Personal runbook. Whole stack (TimescaleDB + FastAPI backend + Next.js
frontend) runs in Docker via one command. Ports bind to `127.0.0.1`
only. Sharing access to other devices goes through Tailscale.

---

## 0. One-time setup

```sh
git clone <this repo>
cd TAIEX
cp .env.example .env       # fill in FINMIND_TOKEN, DISCORD_WEBHOOK_URL, N8N_WEBHOOK_URL
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

---

## 1. Daily start

One command, one terminal:

```sh
cd /Users/raccoon/Desktop/TAIEX
docker compose up
```

Open **http://localhost:3000**.

What happens:

- `db` (TimescaleDB) starts, waits until healthy.
- `backend` starts, runs `alembic upgrade head` (idempotent, safe to
  re-run), then `uvicorn` on port 8000. Healthcheck polls `/health`.
- `frontend` waits for backend healthy, then runs `next dev` on port 3000.
- Logs from all three interleave in the terminal. Ctrl-C stops everything.

During TW market hours (08:45–13:45 Asia/Taipei) the backend ingest loop
polls FinMind every 5 s. Outside market hours it sleeps and emits a
heartbeat.

> **Note:** the FinMind backfill is gone (the new sponsor
> `taiwan_futures_snapshot` endpoint is real-time only). The first time
> you start during market hours, the chart is empty until the first bar
> closes; subsequent days resume from whatever ticks were captured.

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

To wipe the DB completely (resets ticks/signals/alerts):

```sh
docker compose down -v
```

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
uv run pytest -q                              # all tests (no DB needed)
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

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `docker compose up` hangs at "backend Starting" | First-run image pulls | Wait 1–2 min; `docker compose logs backend` to watch progress |
| Backend exits with `ConnectionRefusedError` | DB not healthy yet | Should not happen — backend `depends_on db: service_healthy`. If it does, `docker compose logs db` |
| Frontend shows API errors right after start | Backend healthcheck not green yet | Frontend already waits for backend healthy; otherwise refresh |
| `relation "ticks" does not exist` | Migration didn't run | `docker compose logs backend` — check the alembic line. To force: `docker compose run --rm backend uv run alembic upgrade head` |
| FinMind 400 / 401 | Token expired or wrong tier | Check `FINMIND_TOKEN` in `.env`. Sponsor tier required for `taiwan_futures_snapshot` |
| Chart empty during market hours | First bar hasn't closed yet, or wrong `SYMBOL_SOURCE` | Wait one minute. Confirm `.env` has `SYMBOL_SOURCE=TXF` (or TMF / CDF) |
| zh-TW characters render as boxes | Google Fonts blocked | Allow `fonts.googleapis.com` / `fonts.gstatic.com` in any blocker |
| Tailscale URL fails for invited user | They didn't accept invite, or aren't logged in | Re-send invite from admin console |
| Hot-reload not picking up changes | Editor saving outside `./backend` or `./frontend`, or filesystem watcher quirks on macOS | `docker compose restart backend` (or `frontend`) |
| Want to nuke everything and start clean | — | `docker compose down -v && docker compose up --build` |

---

## 7. Adding a strategy

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

Save the file → uvicorn auto-reloads → refresh the dashboard. The
strategy panel auto-discovers it. Toggle On → signals flow to Discord +
n8n + the in-app log.

---

## 8. Tests

```sh
cd backend && uv run pytest -q
```

14 tests covering indicator math, FinMind snapshot adapter dedupe,
notifier hub fan-out + per-channel failure isolation. None require a
live database.
