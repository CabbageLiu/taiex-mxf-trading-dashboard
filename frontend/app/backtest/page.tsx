"use client";

import {
  CandlestickSeries as _CS,
  LineSeries,
  TickMarkType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";

import { KpiCard } from "@/components/KpiCard";
import { Skeleton } from "@/components/Skeleton";
import { api, type BacktestRequest, type BacktestStats, type BacktestTrade } from "@/lib/api";
import { dict, t, tSide } from "@/lib/i18n";
import { useBacktest } from "@/lib/queries";

// Suppress unused warning for the candlestick import.
void _CS;

const TZ = "Asia/Taipei";
const ACCENT = "#a8773d";
const UP = "#c0392b";
const DOWN = "#3a7d4f";
const INK = "#1f1d1a";

function todayISO(offsetDays = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

function fmtTpe(iso: string): string {
  return new Date(iso).toLocaleString("zh-Hant-TW", {
    timeZone: TZ,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toLocaleString("zh-Hant-TW", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function pnlClass(n: number | null | undefined): string {
  if (n == null) return "";
  if (n > 0) return "pnl-up";
  if (n < 0) return "pnl-down";
  return "";
}

function StatsGrid({ stats }: { stats: BacktestStats }) {
  return (
    <div className="kpi-strip" role="list">
      <KpiCard
        label={t("kpi.pnl")}
        value={fmtNum(stats.pnl_total, 1)}
        sub={t("kpi.unit.points")}
        tone={stats.pnl_total > 0 ? "up" : stats.pnl_total < 0 ? "down" : undefined}
      />
      <KpiCard
        label={t("kpi.winRate")}
        value={
          stats.win_rate != null ? `${(stats.win_rate * 100).toFixed(1)}%` : "—"
        }
      />
      <KpiCard
        label={t("kpi.profitFactor")}
        value={
          stats.profit_factor == null
            ? "—"
            : !Number.isFinite(stats.profit_factor)
              ? "∞"
              : fmtNum(stats.profit_factor)
        }
      />
      <KpiCard
        label={t("kpi.drawdown")}
        value={fmtNum(-Math.abs(stats.max_drawdown), 1)}
        sub={t("kpi.unit.points")}
        tone="down"
      />
      <KpiCard
        label={t("kpi.trades")}
        value={String(stats.trade_count)}
        sub={t("kpi.unit.trades")}
      />
      <KpiCard
        label={t("kpi.avgBars")}
        value={fmtNum(stats.avg_bars_in_trade, 1)}
      />
      <KpiCard
        label={t("kpi.largestWin")}
        value={fmtNum(stats.largest_win, 1)}
        sub={t("kpi.unit.points")}
        tone="up"
      />
      <KpiCard
        label={t("kpi.largestLoss")}
        value={fmtNum(stats.largest_loss, 1)}
        sub={t("kpi.unit.points")}
        tone="down"
      />
    </div>
  );
}

function EquityChart({
  data,
}: {
  data: { ts: string; cumulative_pnl: number }[];
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: { background: { color: "#fbf7ee" }, textColor: INK },
      grid: { vertLines: { color: "#ece5d6" }, horzLines: { color: "#ece5d6" } },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: "#e3dccf",
        tickMarkFormatter: (time: Time, kind: TickMarkType) => {
          const epochSec = typeof time === "number" ? time : 0;
          const d = new Date(epochSec * 1000);
          const opts: Intl.DateTimeFormatOptions = { timeZone: TZ };
          if (kind === TickMarkType.Year) opts.year = "numeric";
          else if (kind === TickMarkType.Month) {
            opts.year = "numeric";
            opts.month = "short";
          } else if (kind === TickMarkType.DayOfMonth) {
            opts.month = "numeric";
            opts.day = "numeric";
          } else {
            opts.hour = "2-digit";
            opts.minute = "2-digit";
            opts.hour12 = false;
          }
          return new Intl.DateTimeFormat("zh-Hant-TW", opts).format(d);
        },
      },
      localization: {
        locale: "zh-Hant-TW",
        timeFormatter: (time: Time) => {
          const epochSec = typeof time === "number" ? time : 0;
          return new Date(epochSec * 1000).toLocaleString("zh-Hant-TW", {
            timeZone: TZ,
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          });
        },
      },
      rightPriceScale: { borderColor: "#e3dccf" },
      crosshair: { vertLine: { color: "#8a8175" }, horzLine: { color: "#8a8175" } },
    });
    chartRef.current = chart;
    seriesRef.current = chart.addSeries(LineSeries, {
      color: ACCENT,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current) return;
    const sorted = [...data].sort(
      (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
    );
    seriesRef.current.setData(
      sorted.map((p) => ({
        time: Math.floor(new Date(p.ts).getTime() / 1000) as Time,
        value: p.cumulative_pnl,
      })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [data]);

  return <div ref={ref} className="bt-equity-canvas" />;
}

function BacktestTradesTable({ trades }: { trades: BacktestTrade[] }) {
  if (trades.length === 0) {
    return <div className="trades-empty">{t("trades.empty")}</div>;
  }
  return (
    <div className="trades-table-wrap">
      <table className="trades-table">
        <thead>
          <tr>
            <th>#</th>
            <th>{t("trades.col.side")}</th>
            <th>{t("bt.cols.entry")}</th>
            <th>{t("bt.cols.exit")}</th>
            <th>{t("bt.cols.bars")}</th>
            <th>{t("trades.col.pnl")}</th>
            <th>{t("bt.cols.reason")}</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((tr) => (
            <tr key={tr.id}>
              <td className="tnum">{tr.id}</td>
              <td>{tSide(tr.side)}</td>
              <td className="tnum">
                <div>{fmtTpe(tr.entry_ts)}</div>
                <div className="muted">@ {fmtNum(tr.entry_price, 0)}</div>
              </td>
              <td className="tnum">
                <div>{fmtTpe(tr.exit_ts)}</div>
                <div className="muted">@ {fmtNum(tr.exit_price, 0)}</div>
              </td>
              <td className="tnum">{tr.bars_held}</td>
              <td className={`tnum ${pnlClass(tr.pnl_points)}`}>
                {tr.pnl_points >= 0 ? "+" : ""}
                {fmtNum(tr.pnl_points, 1)}
              </td>
              <td className="muted bt-reason">{tr.exit_reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BacktestPageInner() {
  const sp = useSearchParams();
  const initialStrat = sp.get("s") ?? "trade_strat_v1";
  const [strategy, setStrategy] = useState<string>(initialStrat);
  const [start, setStart] = useState<string>(todayISO(-7));
  const [end, setEnd] = useState<string>(todayISO(0));

  const stratsQ = useMemo(() => api.strategies(), []);
  const [strategies, setStrategies] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    stratsQ.then((rows) => {
      if (cancelled) return;
      setStrategies(rows.map((r) => r.name));
    }).catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [stratsQ]);

  const [submitted, setSubmitted] = useState<BacktestRequest | null>(null);
  const bt = useBacktest(submitted);
  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted({
      strategy,
      start: new Date(`${start}T00:00:00+08:00`).toISOString(),
      end: new Date(`${end}T00:00:00+08:00`).toISOString(),
    });
  };

  return (
    <div className="bt-page">
      <h1 className="section-title bt-title">{t("bt.title")}</h1>

      <form className="bt-form card-enter" onSubmit={onSubmit}>
        <label className="bt-field">
          <span className="bt-field-label">{t("bt.strategy")}</span>
          <select
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            className="bt-input"
          >
            {strategies.length === 0 && <option value={initialStrat}>{initialStrat}</option>}
            {strategies.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label className="bt-field">
          <span className="bt-field-label">{t("bt.start")}</span>
          <input
            type="date"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className="bt-input"
          />
        </label>
        <label className="bt-field">
          <span className="bt-field-label">{t("bt.end")}</span>
          <input
            type="date"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className="bt-input"
          />
        </label>
        <button
          type="submit"
          className="bt-submit"
          disabled={bt.isFetching}
        >
          {bt.isFetching ? t("bt.running") : t("bt.run")}
        </button>
      </form>

      {bt.isFetching && (
        <div className="bt-loading card-enter">
          <Skeleton width="100%" height={120} />
          <Skeleton width="100%" height={280} />
          <Skeleton width="100%" height={200} />
        </div>
      )}

      {bt.isError && (
        <div className="bt-error" role="alert">
          {t("bt.error")} {bt.error?.message ?? "unknown"}
        </div>
      )}

      {bt.data && !bt.isFetching && (
        <div className="bt-results">
          <div className="bt-meta">
            <span>{bt.data.strategy}</span>
            <span className="muted">·</span>
            <span>{bt.data.symbol}</span>
            <span className="muted">·</span>
            <span>
              {Object.entries(bt.data.bar_counts)
                .map(([r, n]) => `${r}: ${n}`)
                .join(" · ")}
            </span>
          </div>

          <StatsGrid stats={bt.data.stats} />

          <section className="bt-equity-section card-enter">
            <h2 className="section-title">{t("bt.equity")}</h2>
            <div className="bt-equity">
              <EquityChart data={bt.data.equity_curve} />
            </div>
          </section>

          <section className="bt-trades-section card-enter">
            <h2 className="section-title">{t("bt.tradesTitle")}</h2>
            <BacktestTradesTable trades={bt.data.trades} />
          </section>
        </div>
      )}

      {!bt.data && !bt.isFetching && !bt.isError && (
        <div className="bt-empty muted">{t("bt.empty")}</div>
      )}
    </div>
  );
}

void dict; // keep import side-effect

export default function BacktestPage() {
  return (
    <Suspense fallback={null}>
      <BacktestPageInner />
    </Suspense>
  );
}
