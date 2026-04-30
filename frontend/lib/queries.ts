"use client";

import { useMutation, useQuery } from "@tanstack/react-query";

import {
  api,
  type AlertStats,
  type BacktestRequest,
  type BacktestResponse,
  type InsightRequest,
  type InsightResponse,
  type SignalRow,
  type SignalsQuery,
  type StatsQuery,
  type StatusResponse,
  type StrategyState,
  type TestWebhookChannel,
  type TestWebhookResponse,
  type TradeStats,
  type Trade,
  type TradesQuery,
} from "@/lib/api";

const STALE_TRADES = 10_000;

export function useStatus() {
  return useQuery<StatusResponse>({
    queryKey: ["status"],
    queryFn: api.getStatus,
    refetchInterval: 5_000,
    staleTime: 4_000,
    retry: 1,
  });
}

export function useTrades(filter: TradesQuery = {}) {
  return useQuery<Trade[]>({
    queryKey: ["trades", filter],
    queryFn: () => api.getTrades(filter),
    staleTime: STALE_TRADES,
  });
}

export function useTradeStats(filter: StatsQuery = {}) {
  return useQuery<TradeStats>({
    queryKey: ["trade-stats", filter],
    queryFn: () => api.getTradeStats(filter),
    staleTime: STALE_TRADES,
  });
}

// Strategy live state — daily confidence + open position. Polled at 60 s
// because the underlying `_STATE` only mutates on daily / 30 m / 5 m bar
// closes; a faster cadence wastes round-trips.
export function useStrategyState(name: string | null | undefined) {
  return useQuery<StrategyState>({
    queryKey: ["strategy-state", name],
    queryFn: () => api.getStrategyState(name as string),
    enabled: !!name,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

// Backtest — V4 lens-driven query. Pass null to disable; passing the same
// (strategy, symbol, start, end, params) re-uses the cached result so
// /trading and /analysis don't re-run the engine on navigation.
export function useBacktest(req: BacktestRequest | null) {
  return useQuery<BacktestResponse>({
    queryKey: [
      "backtest",
      req?.strategy,
      req?.symbol,
      req?.start,
      req?.end,
      paramsHash(req?.params),
    ],
    queryFn: () => api.runBacktest(req!), // safe: gated by `enabled`
    enabled: req != null && !!req.strategy && !!req.start && !!req.end,
    staleTime: 60_000,
    gcTime: 300_000,
  });
}

function paramsHash(p: Record<string, unknown> | undefined): string {
  if (!p || Object.keys(p).length === 0) return "";
  // Stable JSON; backend cache uses sorted-keys hash for the same purpose.
  return JSON.stringify(
    Object.fromEntries(
      Object.entries(p).sort(([a], [b]) => a.localeCompare(b)),
    ),
  );
}

export function useSignals(params: SignalsQuery = {}) {
  return useQuery<SignalRow[]>({
    queryKey: [
      "signals",
      params.strategy ?? null,
      params.since ?? null,
      params.limit ?? 50,
    ],
    queryFn: () => api.getSignals(params),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}

export function useAlertStats() {
  return useQuery<AlertStats>({
    queryKey: ["alert-stats"],
    queryFn: api.getAlertStats,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}

export function useTestWebhook() {
  return useMutation<TestWebhookResponse, Error, { channel: TestWebhookChannel }>({
    mutationFn: (body) => api.testWebhook(body),
  });
}

/**
 * Manual-trigger mutation for `/insights/strategy`. Not auto-fetched —
 * the analysis page wires the `mutate(...)` call to a button click so we
 * never bill Anthropic on accidental refetch.
 */
export function useInsight() {
  return useMutation<InsightResponse, Error, InsightRequest>({
    mutationFn: (body) => api.postInsight(body),
  });
}
