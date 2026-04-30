"use client";

import { useMemo } from "react";

import type { BacktestStats, BacktestTrade, Trade, TradeStats } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useInsight } from "@/lib/queries";
import { Skeleton } from "./Skeleton";
import type { TradeFilterValue } from "./TradeFilterBar";

type Props = {
  filter: TradeFilterValue;
  stats: TradeStats | undefined;
  trades: Trade[] | undefined;
  isLoading?: boolean;
  // Lens-on inline payload (single-strategy backtest) — when supplied, the
  // generate button posts these rows inline instead of forcing the server to
  // re-query the live `trades` table.
  inlineTrades?: BacktestTrade[];
  inlineStats?: BacktestStats;
  // Comparison mode — when true, the button posts both compareA/compareB
  // payloads. The backend slice 4B accepts the new shape; until landed,
  // the wrapper response error surfaces in the panel as `t("insight.error")`.
  compareMode?: boolean;
  compareA?: { strategy: string | null; stats?: BacktestStats; trades?: BacktestTrade[] };
  compareB?: { strategy: string | null; stats?: BacktestStats; trades?: BacktestTrade[] };
};

function fmtPoints(n: number | null | undefined, opts?: { signed?: boolean }): string | null {
  if (n == null || Number.isNaN(n)) return null;
  const signed = opts?.signed ?? false;
  if (signed) {
    const sign = n > 0 ? "+" : n < 0 ? "" : ""; // negative already prints "-"
    return `${sign}${n.toFixed(1)}`;
  }
  return n.toFixed(1);
}

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString("zh-Hant-TW", {
      timeZone: "Asia/Taipei",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function buildPatternLines(
  stats: TradeStats | undefined,
  trades: Trade[] | undefined,
): Array<{ key: string; label: string; value: string; tone?: "up" | "down" }> {
  if (!stats) return [];
  const lines: Array<{ key: string; label: string; value: string; tone?: "up" | "down" }> = [];

  const avgWin = fmtPoints(stats.pnl_avg_win, { signed: true });
  if (avgWin != null) {
    lines.push({
      key: "avg_win",
      label: "平均獲利",
      value: `${avgWin} ${t("kpi.unit.points")}`,
      tone: "up",
    });
  }

  const avgLoss = fmtPoints(stats.pnl_avg_loss, { signed: true });
  if (avgLoss != null) {
    lines.push({
      key: "avg_loss",
      label: "平均虧損",
      value: `${avgLoss} ${t("kpi.unit.points")}`,
      tone: "down",
    });
  }

  if (stats.avg_hold_seconds != null) {
    const minutes = Math.max(1, Math.ceil(stats.avg_hold_seconds / 60));
    lines.push({
      key: "avg_hold",
      label: "平均持倉",
      value: `${minutes} 分鐘`,
    });
  }

  // Drawdown is reported as a positive magnitude in the stats payload;
  // surface as a negative-signed point delta.
  if (stats.max_drawdown != null && stats.max_drawdown !== 0) {
    const mag = Math.abs(stats.max_drawdown).toFixed(1);
    lines.push({
      key: "max_dd",
      label: "最大回撤",
      value: `-${mag} ${t("kpi.unit.points")}`,
      tone: "down",
    });
  }

  // Closed trades only
  if (trades && trades.length > 0) {
    let longs = 0;
    let shorts = 0;
    for (const tr of trades) {
      if (tr.exit_ts == null) continue;
      if (tr.side === "LONG") longs += 1;
      else if (tr.side === "SHORT") shorts += 1;
    }
    if (longs + shorts > 0) {
      lines.push({
        key: "ratio",
        label: "多 / 空 比",
        value: `${longs} : ${shorts}`,
      });
    }
  }

  if (stats.open_count != null) {
    lines.push({
      key: "open",
      label: "當前未平倉",
      value: `${stats.open_count} 筆`,
    });
  }

  return lines;
}

function parseBullets(content: string): string[] {
  return content
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => s.replace(/^[・·•\-*]\s*/, "")) // ・ · • - *
    .filter(Boolean);
}

function isMissingApiKeyError(err: Error | null): boolean {
  if (!err) return false;
  const msg = err.message ?? "";
  // fetchJson formats errors as `${status} ${body}` — match either the 503 prefix
  // or the literal backend reason string.
  return /^503\b/.test(msg) || /ANTHROPIC_API_KEY/.test(msg);
}

export function TradeInsightPanel(props: Props) {
  const { filter, stats, trades, isLoading = false } = props;
  const insight = useInsight();

  const patternLines = useMemo(
    () => buildPatternLines(stats, trades),
    [stats, trades],
  );

  const patternsBusy = isLoading && patternLines.length === 0;

  const bullets = useMemo(() => {
    if (!insight.data?.content) return [] as string[];
    return parseBullets(insight.data.content);
  }, [insight.data?.content]);

  const canGenerate = props.compareMode
    ? !!(props.compareA?.strategy && props.compareB?.strategy)
    : filter.strategy != null;
  const showLoading = insight.isPending;
  const showResult = !showLoading && insight.isSuccess && bullets.length > 0;
  const errorMsg = insight.isError
    ? isMissingApiKeyError(insight.error as Error)
      ? "伺服器尚未設定 ANTHROPIC_API_KEY"
      : t("insight.error")
    : null;

  const handleGenerate = () => {
    if (props.compareMode && props.compareA && props.compareB) {
      insight.mutate({
        strategy: `${props.compareA.strategy ?? "A"}__vs__${props.compareB.strategy ?? "B"}`,
        compare: true,
        compare_a: {
          strategy: props.compareA.strategy ?? "A",
          stats: props.compareA.stats,
          trades: props.compareA.trades,
        },
        compare_b: {
          strategy: props.compareB.strategy ?? "B",
          stats: props.compareB.stats,
          trades: props.compareB.trades,
        },
      });
      return;
    }
    if (props.inlineTrades) {
      insight.mutate({
        strategy: filter.strategy as string,
        start: filter.start ?? undefined,
        end: filter.end ?? undefined,
        filter: filter.result,
        trades: props.inlineTrades,
        stats: props.inlineStats,
      });
      return;
    }
    if (!canGenerate) return;
    insight.mutate({
      strategy: filter.strategy as string,
      start: filter.start ?? undefined,
      end: filter.end ?? undefined,
      filter: filter.result,
    });
  };

  return (
    <div className="insight-panel">
      {/* Section A — pattern analysis (deterministic, no AI) */}
      <section aria-busy={patternsBusy}>
        <h3 className="section-title">{t("patterns.title")}</h3>
        {patternsBusy ? (
          <div
            style={{ display: "flex", flexDirection: "column", gap: 8, padding: "4px 0" }}
            aria-hidden="true"
          >
            <Skeleton width="60%" height={14} />
            <Skeleton width="48%" height={14} />
            <Skeleton width="54%" height={14} />
            <Skeleton width="40%" height={14} />
          </div>
        ) : patternLines.length === 0 ? (
          <div className="empty">{t("trades.empty")}</div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {patternLines.map((line) => (
              <li
                key={line.key}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  gap: 12,
                  padding: "4px 0",
                  fontSize: "var(--fs-meta)",
                }}
              >
                <span style={{ color: "var(--ink-muted)" }}>{line.label}</span>
                <span
                  style={{
                    fontVariantNumeric: "tabular-nums",
                    color:
                      line.tone === "up"
                        ? "var(--up)"
                        : line.tone === "down"
                          ? "var(--down)"
                          : "var(--ink)",
                  }}
                >
                  {line.value}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Section B — AI insight (manual button) */}
      <section>
        <h3 className="section-title">{t("insight.title")}</h3>

        {showLoading && (
          <>
            <div
              className="empty"
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "4px 0 10px",
              }}
            >
              <Spinner />
              <span>{t("insight.loading")}</span>
            </div>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 8,
                padding: "0 0 4px 18px",
              }}
              aria-hidden="true"
            >
              <Skeleton width="92%" height={13} />
              <Skeleton width="78%" height={13} />
              <Skeleton width="85%" height={13} />
            </div>
          </>
        )}

        {showResult && (
          <>
            <ul>
              {bullets.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
            <div className="meta">
              生成於 {fmtTime(insight.data!.generated_at)}
              {insight.data!.cached ? ` · ${t("insight.cached")}` : ""}
            </div>
          </>
        )}

        {!showLoading && !showResult && (
          <div style={{ textAlign: "center", padding: "8px 0 4px" }}>
            <div className="empty" style={{ marginBottom: 12 }}>
              {errorMsg ?? t("insight.empty")}
            </div>
            <button
              type="button"
              className="btn"
              onClick={handleGenerate}
              disabled={!canGenerate || insight.isPending}
              aria-pressed={false}
              style={{ minHeight: 44, padding: "10px 18px" }}
            >
              {t("insight.generate")}
            </button>
            {!canGenerate && (
              <div
                className="empty"
                style={{ marginTop: 8, fontSize: "var(--fs-caption)" }}
              >
                {props.compareMode ? "請於 URL 設定 s 與 s2" : "先在頂部選擇策略"}
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

function Spinner() {
  return (
    <span
      aria-hidden="true"
      style={{
        display: "inline-block",
        width: 12,
        height: 12,
        border: "2px solid var(--rule)",
        borderTopColor: "var(--accent)",
        borderRadius: "50%",
        animation: "spin 0.8s linear infinite",
      }}
    />
  );
}
