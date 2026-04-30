export type Bar = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  tick_count: number;
};

export type IndicatorSeries = Record<string, Array<{ time: number } & Record<string, number | null>>>;

export type StrategyOut = {
  name: string;
  resolutions: string[];
  params_schema: any;
  enabled: boolean;
  params: Record<string, unknown>;
  channels: string[];
  // V5 Phase B — backend slice A4 surfaces the strategy's human-readable
  // label (e.g. "30分鐘線策略"). Optional + nullable for backward compat
  // with deployments that haven't been redeployed yet.
  display_name?: string | null;
};

export type AlertOut = {
  id: number;
  ts: string;
  signal_id: number | null;
  channel: string;
  status: string;
  http_code: number | null;
  error: string | null;
};

// V2 — status
export type StatusResponse = {
  ok: boolean;
  ingest_running: boolean;
  last_tick_ts: string | null;
  ingest_lag_seconds: number | null;
  strategy_loop_running: boolean;
  position_tracker_running: boolean;
  db_ok: boolean;
  notifiers: { discord: boolean; n8n: boolean; inapp: boolean };
};

// V2 — trades
export type TradeSide = "LONG" | "SHORT";

// V5 Phase A — backend writes indicator snapshots into Trade.payload at the
// moment of entry / exit. Each field is `number | null` because indicators
// can legitimately be unavailable (warm-up window, cross-resolution gaps).
export type TradeIndicators = {
  k: number | null;
  d: number | null;
  macd: number | null;
  signal: number | null;
  hist: number | null;
  plus_di: number | null;
  minus_di: number | null;
  adx: number | null;
};

// V5 Phase A — Trade.payload shape. Documents the known keys without locking
// down the rest (strategies are free to stash extra context like
// `entry_reason`, `exit_reason`, etc., which downstream consumers read via
// the index signature).
export type TradePayload = {
  entry_ind?: TradeIndicators | null;
  exit_ind?: TradeIndicators | null;
  [key: string]: unknown;
};

export type Trade = {
  id: number;
  strategy: string;
  symbol: string;
  side: TradeSide;
  entry_ts: string;
  entry_price: number;
  entry_signal_id: number | null;
  exit_ts: string | null;
  exit_price: number | null;
  exit_signal_id: number | null;
  qty: number;
  pnl_points: number | null;
  hold_seconds: number | null;
  payload: TradePayload;
};

export type TradesQuery = {
  strategy?: string;
  start?: string;
  end?: string;
  result?: "all" | "win" | "loss";
  limit?: number;
};

export type StatsQuery = {
  strategy?: string;
  start?: string;
  end?: string;
};

export type TradeStats = {
  trade_count: number;
  open_count?: number;
  win_count: number;
  loss_count: number;
  win_rate: number | null;
  pnl_total: number;
  pnl_avg_win: number | null;
  pnl_avg_loss: number | null;
  max_drawdown: number;
  avg_hold_seconds: number | null;
};

// V2 — insights (Agent B)
export type InsightRequest = {
  strategy: string;
  start?: string;
  end?: string;
  filter?: "all" | "win" | "loss";
};

export type InsightResponse = {
  cached: boolean;
  generated_at: string;
  content: string;
};

// V4 — extends InsightRequest with optional inline trade payload + stats so
// callers can pre-supply rows instead of forcing the server to re-query.
// Both fields stay optional for backward compatibility with the V2 wrapper.
export type InsightRequestExtended = InsightRequest & {
  trades?: Array<{
    id: number;
    side: string;
    entry_ts: string;
    exit_ts?: string | null;
    entry_price: number;
    exit_price?: number | null;
    pnl_points: number;
  }>;
  stats?: TradeStats;
  // V4 phase 4A — comparison mode. Backend slice 4B will accept these fields
  // and produce a comparative narrative; until then the wrapper transmits and
  // an unrecognized payload returns a non-200 (handled by the panel).
  compare?: boolean;
  compare_a?: {
    strategy: string | null;
    stats?: BacktestStats;
    trades?: BacktestTrade[];
  };
  compare_b?: {
    strategy: string | null;
    stats?: BacktestStats;
    trades?: BacktestTrade[];
  };
};

// V4 — signals (raw signal stream, distinct from `alerts` which is per-channel
// notifier delivery attempts).
export type SignalRow = {
  id: number;
  ts: string;
  symbol: string;
  resolution: string;
  strategy: string;
  side: "LONG" | "SHORT" | "EXIT" | "FLAT";
  price: number | null;
  payload: Record<string, unknown>;
};

export type SignalsQuery = {
  strategy?: string;
  since?: string;
  limit?: number;
};

// V4 — alerts/stats: per-channel rollup counters keyed by channel name.
export type AlertStats = Record<
  string,
  { sent: number; failed: number; last_ts: string | null }
>;

// V4 — admin/test-webhook: synchronous test-fire of a notifier channel.
export type TestWebhookChannel = "discord" | "n8n" | "inapp";
export type TestWebhookResponse = {
  channel: string;
  ok: boolean;
  http_code: number | null;
  error: string | null;
};

// Backtest engine response — Pine-Script-style strategy tester payload.
export type BacktestSignal = {
  ts: string;
  side: "LONG" | "SHORT" | "EXIT" | "FLAT";
  price: number;
  resolution: string;
  reason: string;
  payload: Record<string, unknown>;
};

export type BacktestTrade = {
  id: number;
  side: "LONG" | "SHORT";
  entry_ts: string;
  entry_price: number;
  exit_ts: string;
  exit_price: number;
  pnl_points: number;
  hold_seconds: number;
  bars_held: number;
  entry_reason: string;
  exit_reason: string;
};

export type BacktestStats = TradeStats & {
  profit_factor: number | null;
  largest_win: number | null;
  largest_loss: number | null;
  avg_bars_in_trade: number | null;
};

export type BacktestRequest = {
  strategy: string;
  symbol?: string;
  start: string;
  end: string;
  params?: Record<string, unknown>;
};

export type BacktestResponse = {
  strategy: string;
  symbol: string;
  start: string;
  end: string;
  params: Record<string, unknown>;
  resolutions: string[];
  bar_counts: Record<string, number>;
  signals: BacktestSignal[];
  trades: BacktestTrade[];
  stats: BacktestStats;
  equity_curve: { ts: string; cumulative_pnl: number }[];
};

// Strategy live state — generic shape; trade_strat_v1 populates the optional
// fields below. Strategies without `dump_state` return an empty `state` object.
export type StrategyStatePosition = {
  side: "LONG" | "SHORT";
  entry_price: number;
  entry_ts: string;
};

export type StrategyState = {
  name: string;
  symbol: string;
  state: {
    daily_confidence_long?: number;
    daily_confidence_short?: number;
    daily_last_bucket?: string | null;
    cooldown_left?: number;
    position?: StrategyStatePosition | null;
  };
};

const base = "/api";

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${base}${path}`, init);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// Re-export under the legacy name `jget` — internal helper, may be removed later
const jget = <T,>(path: string) => fetchJson<T>(path);

function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === "") continue;
    q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  bars: (params: { symbol?: string; res: string; limit?: number }) => {
    const q = buildQuery({ symbol: params.symbol, res: params.res, limit: params.limit ?? null });
    return jget<{ symbol: string; resolution: string; bars: Bar[] }>(`/bars${q}`);
  },
  indicators: (params: { symbol?: string; res: string; kinds: string[]; paramSpecs?: { kind: string; params: Record<string, unknown> }[] }) => {
    const q = new URLSearchParams();
    if (params.symbol) q.set("symbol", params.symbol);
    q.set("res", params.res);
    q.set("kinds", params.kinds.join(","));
    if (params.paramSpecs?.length) q.set("params", JSON.stringify(params.paramSpecs));
    return jget<{ symbol: string; resolution: string; series: IndicatorSeries }>(`/indicators?${q}`);
  },
  strategies: () => jget<StrategyOut[]>("/strategies"),
  alerts: (limit = 100) => jget<AlertOut[]>(`/alerts?limit=${limit}`),
  enableStrategy: async (name: string, enabled: boolean) => {
    const r = await fetch(`${base}/strategies/${name}/enable`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  setStrategyParams: async (
    name: string,
    body: { params?: Record<string, unknown>; channels?: string[] },
  ) => {
    const r = await fetch(`${base}/strategies/${name}/params`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },

  // V2 — typed wrappers used by Agent C/D
  getStatus: () => fetchJson<StatusResponse>("/status"),

  getTrades: (params: TradesQuery = {}) => {
    const q = buildQuery({
      strategy: params.strategy,
      start: params.start,
      end: params.end,
      result: params.result,
      limit: params.limit ?? 200,
    });
    return fetchJson<Trade[]>(`/trades${q}`);
  },

  getTradeStats: (params: StatsQuery = {}) => {
    const q = buildQuery({
      strategy: params.strategy,
      start: params.start,
      end: params.end,
    });
    return fetchJson<TradeStats>(`/trades/stats${q}`);
  },

  postInsight: (body: InsightRequest | InsightRequestExtended) =>
    fetchJson<InsightResponse>("/insights/strategy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  getStrategyState: (name: string) =>
    fetchJson<StrategyState>(`/strategies/${encodeURIComponent(name)}/state`),

  runBacktest: (body: BacktestRequest) =>
    fetchJson<BacktestResponse>("/backtest/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // V4 — raw signal feed (distinct from /alerts which is delivery attempts).
  getSignals: (params: SignalsQuery = {}) => {
    const q = buildQuery({
      strategy: params.strategy,
      since: params.since,
      limit: params.limit ?? 50,
    });
    return fetchJson<SignalRow[]>(`/signals${q}`);
  },

  // V4 — per-channel notifier rollup.
  getAlertStats: () => fetchJson<AlertStats>("/alerts/stats"),

  // V4 — fire a test webhook against the named channel.
  testWebhook: (body: { channel: TestWebhookChannel }) =>
    fetchJson<TestWebhookResponse>("/admin/test-webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};

// Standalone exports for direct ergonomic use (Agent D)
export function getStatus() { return api.getStatus(); }
export function getTrades(params: TradesQuery = {}) { return api.getTrades(params); }
export function getTradeStats(params: StatsQuery = {}) { return api.getTradeStats(params); }
export function postInsight(body: InsightRequest | InsightRequestExtended) {
  return api.postInsight(body);
}
