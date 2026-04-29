"use client";

import { useMutation, useQuery } from "@tanstack/react-query";

import {
  api,
  type InsightRequest,
  type InsightResponse,
  type StatsQuery,
  type StatusResponse,
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
