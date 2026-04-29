"use client";

import { useMutation, useQuery } from "@tanstack/react-query";

import {
  api,
  type BacktestRequest,
  type BacktestResponse,
  type InsightRequest,
  type InsightResponse,
  type StatsQuery,
  type StatusResponse,
  type StrategyState,
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

// Backtest — manual mutation, run on form submit.
export function useBacktest() {
  return useMutation<BacktestResponse, Error, BacktestRequest>({
    mutationFn: (body) => api.runBacktest(body),
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
