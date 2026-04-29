"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

import { KpiCard } from "@/components/KpiCard";
import { TradesTable } from "@/components/TradesTable";
import {
  TradeFilterBar,
  type TradeFilterValue,
} from "@/components/TradeFilterBar";
import { TradeInsightPanel } from "@/components/TradeInsightPanel";
import { useTrades, useTradeStats } from "@/lib/queries";
import { t } from "@/lib/i18n";

function isoDateUTC(d: Date): string {
  // YYYY-MM-DD in UTC; sufficient for date-range filters.
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function defaultFilter(strategy: string | null): TradeFilterValue {
  const today = new Date();
  const past = new Date(today);
  past.setUTCDate(past.getUTCDate() - 30);
  return {
    strategy,
    start: isoDateUTC(past),
    end: isoDateUTC(today),
    result: "all",
  };
}

function fmtPct(n: number | null | undefined): string {
  if (n == null) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function fmtSignedPoints(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n === 0) return "0.0";
  const sign = n > 0 ? "+" : ""; // negative gets "-" from toFixed
  return `${sign}${n.toFixed(1)}`;
}

export default function AnalysisPage() {
  // useSearchParams() forces dynamic rendering and must sit under a Suspense
  // boundary on Next 15 — wrap the body in <Suspense> here.
  return (
    <Suspense fallback={null}>
      <AnalysisContent />
    </Suspense>
  );
}

function AnalysisContent() {
  const search = useSearchParams();
  const strategyFromUrl = search.get("s");

  const [filter, setFilter] = useState<TradeFilterValue>(() =>
    defaultFilter(strategyFromUrl),
  );

  // Keep filter.strategy in sync when the URL ?s= changes (global selector
  // in the shell header). One-way sync: URL → local state.
  useEffect(() => {
    setFilter((prev) =>
      prev.strategy === strategyFromUrl ? prev : { ...prev, strategy: strategyFromUrl },
    );
  }, [strategyFromUrl]);

  const tradesQuery = useTrades({
    strategy: filter.strategy ?? undefined,
    start: filter.start ?? undefined,
    end: filter.end ?? undefined,
    result: filter.result,
  });

  const statsQuery = useTradeStats({
    strategy: filter.strategy ?? undefined,
    start: filter.start ?? undefined,
    end: filter.end ?? undefined,
  });

  const stats = statsQuery.data;
  const trades = tradesQuery.data;
  const statsLoading = statsQuery.isLoading || statsQuery.isPending;

  const winRateTone =
    stats?.win_rate == null
      ? "neutral"
      : stats.win_rate >= 0.5
        ? "up"
        : "down";

  const pnlTone =
    stats?.pnl_total == null
      ? "neutral"
      : stats.pnl_total > 0
        ? "up"
        : stats.pnl_total < 0
          ? "down"
          : "neutral";

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "1fr 340px",
        gap: 24,
        padding: 24,
        flex: 1,
        minHeight: 0,
        alignItems: "start",
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <div className="kpi-strip">
          <KpiCard
            label={t("kpi.winRate")}
            value={fmtPct(stats?.win_rate)}
            sub={
              stats
                ? `${stats.win_count} / ${stats.trade_count} ${t("kpi.unit.trades")}`
                : "—"
            }
            tone={winRateTone}
            isLoading={statsLoading}
          />
          <KpiCard
            label={t("kpi.trades")}
            value={stats ? `${stats.trade_count}` : "—"}
            sub={
              stats
                ? `${t("filter.win")} ${stats.win_count} · ${t("filter.loss")} ${stats.loss_count}`
                : undefined
            }
            tone="neutral"
            isLoading={statsLoading}
          />
          <KpiCard
            label={t("kpi.pnl")}
            value={
              stats
                ? `${stats.pnl_total > 0 ? "▲ " : stats.pnl_total < 0 ? "▼ " : ""}${fmtSignedPoints(stats.pnl_total)}`
                : "—"
            }
            sub={t("kpi.unit.points")}
            tone={pnlTone}
            isLoading={statsLoading}
          />
          <KpiCard
            label={t("kpi.drawdown")}
            value={
              stats?.max_drawdown != null
                ? `▼ -${Math.abs(stats.max_drawdown).toFixed(1)}`
                : "—"
            }
            sub={t("kpi.unit.points")}
            tone={
              stats?.max_drawdown != null && stats.max_drawdown !== 0
                ? "down"
                : "neutral"
            }
            isLoading={statsLoading}
          />
        </div>

        <TradeFilterBar filter={filter} onChange={setFilter} />

        <div style={{ overflowX: "auto" }}>
          <TradesTable
            trades={trades}
            isLoading={tradesQuery.isLoading || tradesQuery.isPending}
          />
        </div>
      </div>

      <aside style={{ position: "sticky", top: 24 }}>
        <TradeInsightPanel
          filter={filter}
          stats={stats}
          trades={trades}
          isLoading={statsLoading || tradesQuery.isLoading || tradesQuery.isPending}
        />
      </aside>
    </div>
  );
}
