"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { KpiCard, type KpiTone } from "@/components/KpiCard";
import { StrategyDescription } from "@/components/StrategyDescription";
import { TradesTable } from "@/components/TradesTable";
import {
  TradeFilterBar,
  type TradeFilterValue,
} from "@/components/TradeFilterBar";
import { TradeInsightPanel } from "@/components/TradeInsightPanel";
import { useBacktest, useTrades, useTradeStats } from "@/lib/queries";
import { useLens, type UseLensReturn } from "@/lib/lens";
import { t } from "@/lib/i18n";
import {
  api,
  type BacktestResponse,
  type BacktestStats,
  type BacktestTrade,
  type Trade,
  type TradeStats,
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

function LensBar({ lens }: { lens: UseLensReturn }) {
  const stratsQ = useQuery({
    queryKey: ["strategies"],
    queryFn: api.strategies,
    refetchInterval: 30_000,
  });
  const stratNames = useMemo(
    () => (stratsQ.data ?? []).map((r) => r.name),
    [stratsQ.data],
  );

  // Sensible default range when the lens has none yet.
  const today = new Date();
  const past30 = new Date(today);
  past30.setUTCDate(past30.getUTCDate() - 30);
  const fmtDate = (d: Date) => d.toISOString().slice(0, 10);
  const start = lens.start ?? "";
  const end = lens.end ?? "";

  const onStrategy = (v: string) => {
    lens.setStrategy(v || null);
    if (lens.start == null || lens.end == null) {
      lens.setRange(fmtDate(past30), fmtDate(today));
    }
  };
  const onSecondary = (v: string) => {
    lens.setSecondaryStrategy(v || null);
  };
  const toggleCompare = () => {
    const next = !lens.compare;
    if (next && !lens.secondaryStrategy) {
      // Pick the first registered strategy that isn't the primary as a default.
      const fallback = stratNames.find((n) => n !== lens.strategy) ?? null;
      if (fallback) lens.setSecondaryStrategy(fallback);
    }
    lens.setCompare(next);
  };

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: 12,
        padding: "12px 24px",
        borderBottom: "1px solid var(--rule)",
        background: "var(--panel)",
        fontSize: "var(--fs-meta)",
      }}
    >
      <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
        <span style={{ color: "var(--ink-muted)" }}>{t("panel_strategies")}</span>
        <select
          value={lens.strategy ?? ""}
          onChange={(e) => onStrategy(e.target.value)}
          style={{ minHeight: 32, padding: "2px 8px" }}
        >
          <option value="">—</option>
          {stratNames.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </label>
      {lens.compare && (
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "var(--ink-muted)" }}>vs</span>
          <select
            value={lens.secondaryStrategy ?? ""}
            onChange={(e) => onSecondary(e.target.value)}
            style={{ minHeight: 32, padding: "2px 8px" }}
          >
            <option value="">—</option>
            {stratNames.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      )}
      <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
        <span style={{ color: "var(--ink-muted)" }}>{t("filter.dateRange")}</span>
        <input
          type="date"
          value={start}
          onChange={(e) => lens.setRange(e.target.value || null, lens.end)}
          aria-label="lens start"
        />
        <span style={{ color: "var(--ink-muted)" }}>–</span>
        <input
          type="date"
          value={end}
          onChange={(e) => lens.setRange(lens.start, e.target.value || null)}
          aria-label="lens end"
        />
      </label>
      <button
        type="button"
        className="btn"
        aria-pressed={lens.compare}
        onClick={toggleCompare}
        disabled={!lens.strategy}
        style={{ minHeight: 32, padding: "4px 12px" }}
        title={!lens.strategy ? "先選擇主策略" : ""}
      >
        {t("compare.toggle")}
      </button>
      {lens.isActive && (
        <button
          type="button"
          className="btn"
          onClick={() => lens.reset()}
          style={{ minHeight: 32, padding: "4px 12px", marginLeft: "auto" }}
        >
          重置
        </button>
      )}
    </div>
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

  let body: React.ReactNode;
  if (!lens.isActive) {
    body = (
      <LiveView
        filter={filter}
        setFilter={setFilter}
        liveTradesQ={liveTradesQ}
        liveStatsQ={liveStatsQ}
      />
    );
  } else if (compare) {
    body = <CompareView lens={lens} btQA={btQA} btQB={btQB} />;
  } else {
    body = <SingleLensView lens={lens} btQ={btQA} filter={filter} />;
  }
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
      }}
    >
      <LensBar lens={lens} />
      {body}
    </div>
  );
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

      <aside
        style={{
          position: "sticky",
          top: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <TradeInsightPanel
          filter={filter}
          stats={stats}
          trades={trades}
          isLoading={statsLoading || liveTradesQ.isLoading || liveTradesQ.isPending}
        />
        <StrategyDescription withSectionTitle />
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

      <aside
        style={{
          position: "sticky",
          top: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <TradeInsightPanel
          filter={{ ...filter, strategy }}
          stats={tradeStatsCompat}
          trades={adaptedTrades}
          isLoading={isLoading}
          inlineTrades={trades}
          inlineStats={stats}
        />
        <StrategyDescription withSectionTitle />
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

      <StrategyDescription withSectionTitle />
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
