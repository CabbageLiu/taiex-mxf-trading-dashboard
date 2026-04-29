# TAIEX V3 — Backlog

Open items deferred from V2. Sourced from the V2 plan's Security section, the Codex review against the V2 diff, and notes accumulated during V2 implementation. Group by theme; pick whichever bundle ships first based on operating context.

## Security hardening (highest priority)

These were knowingly deferred in V2 because the deploy is solo / Tailscale-only. They become must-fix the moment a second user, a public hostname, or shared credentials enter the picture.

- **CORS lockdown.** `backend/app/main.py` still uses `allow_origins=["*"]`, `allow_methods=["*"]`. Restrict to `http://localhost:3000` plus the Tailscale Serve hostname. Read the allowed list from `Settings.allowed_origins: list[str]`.
- **Auth on mutating endpoints.** `POST /strategies/{name}/enable`, `PATCH /strategies/{name}/params`, and `POST /insights/strategy` accept anonymous calls. Wire `Settings.alert_secret` (already present, currently unused) as a required `X-API-Key` header on every mutating route via a small FastAPI dependency. Reject with 401 when header missing or mismatched.
- **Reverse-proxy IP gap in rate limiter.** `backend/app/api/routes/insights.py:_client_ip` already honours `X-Forwarded-For`, but if the deployment ever sits behind a proxy that does not set that header, the rate limit collapses to one bucket (the proxy IP). Make the trusted-proxy assumption explicit: parse `Forwarded` / `X-Forwarded-For` only when the immediate peer is in a configured `trusted_proxies` CIDR list.
- **Anthropic spend ceiling.** Frontend disable-on-pending + 5/min/(strategy, ip) bucket guards individual users. Add a *global* daily token-budget check (sum of `usage.input_tokens + usage.output_tokens` from each `messages.create` response, rolling 24 h) that hard-fails new requests once the configured budget is exhausted. Surface remaining budget on `/status`.

## Observability

- `/status` returns booleans + `last_tick_ts` but does not expose Prometheus / OpenTelemetry metrics. Add a `/metrics` endpoint with at minimum: ingest tick rate, signal rate per strategy, trade open/close counters, insight cache hit ratio, Anthropic request latency histogram.
- Structured JSON logs are ad-hoc. Settle on `structlog` with one logger config in `app/main.py`.
- The position tracker logs duplicate open rows on rehydrate (V2 fix); promote that log to a metric so an operator can alert on `position_tracker_duplicate_rehydrate_total > 0`.

## Trade attribution depth

V2 ships round-trip pairing with `pnl_points`. Real trading needs:

- **Fees and slippage.** `Trade.payload` reserves room (`fees`, `slippage`) but the report column is raw points. Add `pnl_points_net` derived at close, plus a `fees_per_contract` setting (basis-points). Update `compute_stats` to expose both gross and net.
- **Position sizing.** `qty = 1.0` is hard-coded in `PositionTracker._open_trade`. Either (a) read qty from the strategy's signal payload, or (b) introduce a per-strategy sizing module.
- **Manual trade override.** Add `PATCH /trades/{id}` that allows correcting `entry_price` / `exit_price` / closing an erroneously open trade. Audit-log every override into `Trade.payload.history`.
- **Race-condition note.** Codex flagged a theoretical race between strategy_loop emitting two signals very close in time and the tracker's serialised `_handle()`. The partial unique index added in V2 prevents DB-level double-open, but a flipped trade still has a millisecond-scale window between `_close` commit and `_open_trade` commit where `/trades/stats` could observe an open_count of zero. Wrap the flip in a single transaction.

## Backtesting

V2 only attributes live signals. V3 should let the user replay a strategy against historical bars to compare its hypothetical PnL curve against the realised one.

- New module `app/backtest/runner.py` that takes `(strategy_name, params, start, end)` and returns the same `Trade` rows shape, persisted into a separate `backtest_trades` table (or a `mode` column on `trades` to discriminate live vs. backtest).
- Frontend `/analysis` filter gains a `live | backtest | both` toggle.

## Strategy authoring UX

- The `Pine-script-like` text editor was explicitly cut from V2. Bring it back as a Monaco-based editor that compiles a small DSL down to a registered `Strategy` subclass at runtime. Sandboxed exec, AST allowlist, no I/O. Persistence in a new `user_strategies` table.
- Surface `params_schema` validation errors inline in `StrategyParamsPopover` instead of generic toast on submit.

## Multi-user / web hosting

- Login + session auth (intentionally cut from V2).
- Per-user `enabled` strategies and per-user notifier channel config (right now `strategy_config.channels` is global).
- Tenant scoping on `signals`, `trades`, `alerts`.

## AI insights — quality and cost

- **Cache fingerprint** uses sorted `(id, pnl_points)` tuples as of V2 (codex fix). Validate over a few weeks that the hit rate is high enough to keep cost predictable; if not, widen the fingerprint to a coarser bucket (e.g. round timestamps to the hour).
- **Streaming responses.** Switch to `client.messages.stream(...)` and pipe tokens to the frontend so users see bullets appearing instead of a 2-3 s blank wait. Server-Sent Events fits well with the existing infra.
- **Multilingual.** UI hard-codes 繁體中文 output. If an English-speaking operator wants the same panel, expose `lang` on the request and tweak the system prompt.
- **Prompt-injection regression test.** V2 has a unit test asserting `payload.reason` is JSON-escaped. Add an integration-style fuzz test that pushes a battery of known prompt-injection payloads through the full pipeline and verifies the model never echoes the system prompt.
- **Prompt-cache breakeven.** Sonnet 4.6's minimum cacheable prefix is 2048 tokens; the V2 system prompt is shorter, so the `cache_control` marker is currently a no-op in practice. Once the prompt grows (e.g. by including more context), confirm via `usage.cache_read_input_tokens` that cache hits are actually firing.

## Frontend polish

- **Empty / loading states across all panels.** TradesTable has skeleton + empty; KPI strip and TradeInsightPanel only have empty. Add skeletons.
- **Mobile / tablet layout.** V2 explicitly targets desktop. Audit `/analysis` and `/trading` against 768 px and 1024 px breakpoints; the right-rail panels collapse below 1024 px.
- **Chart pane teardown logging.** Codex called out that `Chart.tsx` swallows `removePane()` errors. Add a `console.warn` so a future regression is observable in dev tools.
- **Reduce re-renders.** `Chart.tsx` builds the crosshair lookup map inside an effect that runs on every series update. Memoise per-series, or move the map into a ref.
- **Accessibility audit.** Status pill, strategy combobox, and indicator toggles need full keyboard + ARIA verification (UX rule pack `accessibility` from `ui-ux-pro-max`).

## Schema / migrations

- `0002_trades.py` does not index FK columns `entry_signal_id` / `exit_signal_id`. If joining trades back to signals becomes common (e.g. for the manual-override flow above), add `ix_trades_entry_signal_id` and `ix_trades_exit_signal_id`.
- `compute_stats` caps the input slice at 1000 rows in `/trades/stats`. Either bump the cap, or paginate the drawdown computation, or stream rows server-side. Document the cap in the route's response.

## Testing

- Add a docker-compose-based end-to-end test (`testcontainers-python`) that spins TimescaleDB, runs alembic, fires synthetic ticks, and asserts that `trades` rows materialise with correct PnL — the only V2 test that requires a live DB.
- Frontend lacks any test suite. Vitest + React Testing Library on the components Agent D shipped.

## Out of scope for V3 (backlog floor)

Listed here so they are not silently dropped:

- Real broker integration (Shioaji, Capital, etc.) — V3 still pulls FinMind snapshots.
- Order execution from a signal — V3 is observation-only.
- Mobile native app — web is the supported surface.
