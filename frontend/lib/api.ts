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

const base = "/api";

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`${base}${path}`);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export const api = {
  bars: (params: { symbol?: string; res: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params.symbol) q.set("symbol", params.symbol);
    q.set("res", params.res);
    if (params.limit != null) q.set("limit", String(params.limit));
    return jget<{ symbol: string; resolution: string; bars: Bar[] }>(`/bars?${q}`);
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
};
