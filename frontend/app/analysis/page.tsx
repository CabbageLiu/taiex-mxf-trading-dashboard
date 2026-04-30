"use client";

import { Suspense, useEffect, useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

import { KpiCard, type KpiTone } from "@/components/KpiCard";
import { TradesTable } from "@/components/TradesTable";
import {
  TradeFilterBar,
  type TradeFilterValue,
} from "@/components/TradeFilterBar";
import { TradeInsightPanel } from "@/components/TradeInsightPanel";
import { useBacktest, useTrades, useTradeStats } from "@/lib/queries";
import { useLens, type UseLensReturn } from "@/lib/lens";
import { t } from "@/lib/i18n";
import type {
  BacktestResponse,
  BacktestStats,
  BacktestTrade,
  Trade,
  TradeStats,
} from "@/lib/api";

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

function fmtProfitFactor(pf: number | null | undefined): string {
  if (pf == null) return "—";
  if (!Number.isFinite(pf)) return "∞";
  return pf.toFixed(2);
}

function pnlTone(n: number | null | undefined): KpiTone {
  if (n == null) return "neutral";
  if (n > 0) return "up";
  if (n < 0) return "down";
  return "neutral";
}

function winTone(n: number | null | undefined): KpiTone {
  if (n == null) return "neutral";
  return n >= 0.5 ? "up" : "down";
}

/**
 * Adapt backtest trade rows into the live `Trade` shape so the existing
 * `TradesTable` can render them without a parallel component.
 */
function btTradesToLive(
  strategy: string,
  trades: BacktestTrade[] | undefined,
): Trade[] {
  if (!trades) return [];
  return trades.map(
    (tr): Trade => ({
      id: tr.id,
      strategy,
      symbol: "",
      side: tr.side,
      entry_ts: tr.entry_ts,
      entry_price: tr.entry_price,
      entry_signal_id: null,
      exit_ts: tr.exit_ts,
      exit_price: tr.exit_price,
      exit_signal_id: null,
      qty: 1,
      pnl_points: tr.pnl_points,
      hold_seconds: tr.hold_seconds,
      payload: {
        bars_held: tr.bars_held,
        entry_reason: tr.entry_reason,
        exit_reason: tr.exit_reason,
      },
    }),
  );
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
  const lens = useLens();
  const compare = lens.compare && !!lens.secondaryStrategy;

  // Lens-driven backtest queries — null disables. Two slots so compare mode
  // can run both strategies in parallel (each is independently cached on the
  // (strategy, start, end, params) key).
  const btReqA =
    lens.isActive && lens.start && lens.end && lens.strategy
      ? { strategy: lens.strategy, start: lens.start, end: lens.end }
      : null;
  const btReqB =
    compare && lens.secondaryStrategy && lens.start && lens.end
      ? { strategy: lens.secondaryStrategy, start: lens.start, end: lens.end }
      : null;

  const btQA = useBacktest(btReqA);
  const btQB = useBacktest(btReqB);

  // Lens-off live filter state (existing behavior).
  const [filter, setFilter] = useState<TradeFilterValue>(() =>
    defaultFilter(lens.strategy),
  );
  useEffect(() => {
    setFilter((prev) =>
      prev.strategy === lens.strategy ? prev : { ...prev, strategy: lens.strategy },
    );
  }, [lens.strategy]);

  const liveTradesQ = useTrades({
    strategy: filter.strategy ?? undefined,
    start: filter.start ?? undefined,
    end: filter.end ?? undefined,
    result: filter.result,
  });
  const liveStatsQ = useTradeStats({
    strategy: filter.strategy ?? undefined,
    start: filter.start ?? undefined,
    end: filter.end ?? undefined,
  });

  if (!lens.isActive) {
    return (
      <LiveView
        filter={filter}
        setFilter={setFilter}
        liveTradesQ={liveTradesQ}
        liveStatsQ={liveStatsQ}
      />
    );
  }
  if (compare) {
    return <CompareView lens={lens} btQA={btQA} btQB={btQB} />;
  }
  return <SingleLensView lens={lens} btQ={btQA} filter={filter} />;
}

// ---------------------------------------------------------------------------
// Lens-off live view — preserves the V2 behavior unchanged.
// ---------------------------------------------------------------------------

type LiveViewProps = {
  filter: TradeFilterValue;
  setFilter: (next: TradeFilterValue) => void;
  liveTradesQ: UseQueryResult<Trade[]>;
  liveStatsQ: UseQueryResult<TradeStats>;
};

function LiveView({ filter, setFilter, liveTradesQ, liveStatsQ }: LiveViewProps) {
  const stats = liveStatsQ.data;
  const trades = liveTradesQ.data;
  const statsLoading = liveStatsQ.isLoading || liveStatsQ.isPending;

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
            tone={winTone(stats?.win_rate)}
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
            tone={pnlTone(stats?.pnl_total)}
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
            isLoading={liveTradesQ.isLoading || liveTradesQ.isPending}
          />
        </div>
      </div>

      <aside style={{ position: "sticky", top: 24 }}>
        <TradeInsightPanel
          filter={filter}
          stats={stats}
          trades={trades}
          isLoading={statsLoading || liveTradesQ.isLoading || liveTradesQ.isPending}
        />
      </aside>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Lens-on single-strategy backtest view.
// ---------------------------------------------------------------------------

type SingleLensViewProps = {
  lens: UseLensReturn;
  btQ: UseQueryResult<BacktestResponse>;
  filter: TradeFilterValue;
};

function SingleLensView({ lens, btQ, filter }: SingleLensViewProps) {
  const stats = btQ.data?.stats;
  const trades = btQ.data?.trades;
  const isLoading = btQ.isLoading || btQ.isPending;
  const errored = btQ.isError;
  const strategy = lens.strategy ?? "";

  // Adapt backtest rows into the shape TradesTable expects.
  const adaptedTrades = btTradesToLive(strategy, trades);

  // Adapt BacktestStats → TradeStats for the insight panel deterministic
  // pattern block (it only reads TradeStats fields).
  const tradeStatsCompat: TradeStats | undefined = stats
    ? {
        trade_count: stats.trade_count,
        open_count: stats.open_count,
        win_count: stats.win_count,
        loss_count: stats.loss_count,
        win_rate: stats.win_rate,
        pnl_total: stats.pnl_total,
        pnl_avg_win: stats.pnl_avg_win,
        pnl_avg_loss: stats.pnl_avg_loss,
        max_drawdown: stats.max_drawdown,
        avg_hold_seconds: stats.avg_hold_seconds,
      }
    : undefined;

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
        <header
          style={{
            display: "flex",
            gap: 12,
            alignItems: "baseline",
            flexWrap: "wrap",
          }}
        >
          <h2
            style={{
              fontSize: "var(--fs-section)",
              fontWeight: "var(--fw-bold)",
              margin: 0,
              color: "var(--strategy-1)",
            }}
          >
            {t("compare.lensSubtitle")}
            {strategy}
          </h2>
          <span style={{ color: "var(--ink-muted)", fontVariantNumeric: "tabular-nums" }}>
            {lens.start} – {lens.end}
          </span>
        </header>

        {errored ? (
          <div className="empty" style={{ padding: "16px 0" }}>
            {t("bt.error")}
            {(btQ.error as Error)?.message ?? ""}
          </div>
        ) : null}

        <BacktestKpiStrip stats={stats} isLoading={isLoading} />

        <div style={{ overflowX: "auto" }}>
          <TradesTable trades={adaptedTrades} isLoading={isLoading} />
        </div>
      </div>

      <aside style={{ position: "sticky", top: 24 }}>
        <TradeInsightPanel
          filter={{ ...filter, strategy }}
          stats={tradeStatsCompat}
          trades={adaptedTrades}
          isLoading={isLoading}
          inlineTrades={trades}
          inlineStats={stats}
        />
      </aside>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Lens-on side-by-side compare view.
// ---------------------------------------------------------------------------

type CompareViewProps = {
  lens: UseLensReturn;
  btQA: UseQueryResult<BacktestResponse>;
  btQB: UseQueryResult<BacktestResponse>;
};

function CompareView({ lens, btQA, btQB }: CompareViewProps) {
  const insightLoading =
    btQA.isLoading || btQA.isPending || btQB.isLoading || btQB.isPending;
  // The insight panel still wants a TradeFilterValue; synthesize one from the
  // lens — it is unused in compareMode except for hand-off into the deterministic
  // pattern block (which reads stats / trades from props anyway).
  const syntheticFilter: TradeFilterValue = {
    strategy: lens.strategy,
    start: lens.start,
    end: lens.end,
    result: "all",
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 16,
        padding: 24,
        flex: 1,
        minHeight: 0,
      }}
    >
      <header
        style={{
          display: "flex",
          gap: 12,
          alignItems: "baseline",
          flexWrap: "wrap",
        }}
      >
        <h2
          style={{
            fontSize: "var(--fs-section)",
            fontWeight: "var(--fw-bold)",
            margin: 0,
          }}
        >
          {t("compare.title")}
        </h2>
        <span style={{ color: "var(--ink-muted)", fontVariantNumeric: "tabular-nums" }}>
          {lens.start} – {lens.end}
        </span>
      </header>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 16,
          minWidth: 0,
        }}
      >
        <CompareColumn
          title={lens.strategy ?? ""}
          stratColorVar="--strategy-1"
          bt={btQA}
        />
        <CompareColumn
          title={lens.secondaryStrategy ?? ""}
          stratColorVar="--strategy-2"
          bt={btQB}
        />
      </div>

      <TradeInsightPanel
        filter={syntheticFilter}
        stats={undefined}
        trades={undefined}
        isLoading={insightLoading}
        compareMode
        compareA={{
          strategy: lens.strategy,
          stats: btQA.data?.stats,
          trades: btQA.data?.trades,
        }}
        compareB={{
          strategy: lens.secondaryStrategy,
          stats: btQB.data?.stats,
          trades: btQB.data?.trades,
        }}
      />
    </div>
  );
}

type CompareColumnProps = {
  title: string;
  stratColorVar: string;
  bt: UseQueryResult<BacktestResponse>;
};

function CompareColumn({ title, stratColorVar, bt }: CompareColumnProps) {
  const stats = bt.data?.stats;
  const trades = bt.data?.trades;
  const isLoading = bt.isLoading || bt.isPending;
  const adapted = btTradesToLive(title, trades);

  return (
    <section
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 12,
        minWidth: 0,
      }}
    >
      <h3
        className="section-title"
        style={{ color: `var(${stratColorVar})`, margin: 0 }}
      >
        {title}
      </h3>

      {bt.isError ? (
        <div className="empty" style={{ padding: "8px 0" }}>
          {t("bt.error")}
          {(bt.error as Error)?.message ?? ""}
        </div>
      ) : null}

      <div
        className="kpi-strip"
        style={{
          // Stack 2x2 in compare so each side fits its half-column.
          gridTemplateColumns: "1fr 1fr",
        }}
      >
        <KpiCard
          label={t("kpi.pnl")}
          value={
            stats
              ? `${stats.pnl_total > 0 ? "▲ " : stats.pnl_total < 0 ? "▼ " : ""}${fmtSignedPoints(stats.pnl_total)}`
              : "—"
          }
          sub={t("kpi.unit.points")}
          tone={pnlTone(stats?.pnl_total)}
          isLoading={isLoading}
        />
        <KpiCard
          label={t("kpi.winRate")}
          value={fmtPct(stats?.win_rate)}
          sub={
            stats
              ? `${stats.win_count} / ${stats.trade_count} ${t("kpi.unit.trades")}`
              : "—"
          }
          tone={winTone(stats?.win_rate)}
          isLoading={isLoading}
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
          isLoading={isLoading}
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
          isLoading={isLoading}
        />
      </div>

      <div style={{ overflowX: "auto" }}>
        <TradesTable trades={adapted} isLoading={isLoading} />
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// 8-card backtest KPI strip used by the single-strategy lens view.
// ---------------------------------------------------------------------------

type BacktestKpiStripProps = {
  stats: BacktestStats | undefined;
  isLoading: boolean;
};

function BacktestKpiStrip({ stats, isLoading }: BacktestKpiStripProps) {
  return (
    <div className="kpi-strip">
      <KpiCard
        label={t("kpi.pnl")}
        value={
          stats
            ? `${stats.pnl_total > 0 ? "▲ " : stats.pnl_total < 0 ? "▼ " : ""}${fmtSignedPoints(stats.pnl_total)}`
            : "—"
        }
        sub={t("kpi.unit.points")}
        tone={pnlTone(stats?.pnl_total)}
        isLoading={isLoading}
      />
      <KpiCard
        label={t("kpi.winRate")}
        value={fmtPct(stats?.win_rate)}
        sub={
          stats
            ? `${stats.win_count} / ${stats.trade_count} ${t("kpi.unit.trades")}`
            : undefined
        }
        tone={winTone(stats?.win_rate)}
        isLoading={isLoading}
      />
      <KpiCard
        label={t("kpi.profitFactor")}
        value={fmtProfitFactor(stats?.profit_factor)}
        tone="neutral"
        isLoading={isLoading}
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
        isLoading={isLoading}
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
        isLoading={isLoading}
      />
      <KpiCard
        label={t("kpi.avgBars")}
        value={
          stats?.avg_bars_in_trade != null
            ? stats.avg_bars_in_trade.toFixed(1)
            : "—"
        }
        tone="neutral"
        isLoading={isLoading}
      />
      <KpiCard
        label={t("kpi.largestWin")}
        value={fmtSignedPoints(stats?.largest_win)}
        sub={t("kpi.unit.points")}
        tone="up"
        isLoading={isLoading}
      />
      <KpiCard
        label={t("kpi.largestLoss")}
        value={fmtSignedPoints(stats?.largest_loss)}
        sub={t("kpi.unit.points")}
        tone="down"
        isLoading={isLoading}
      />
    </div>
  );
}
