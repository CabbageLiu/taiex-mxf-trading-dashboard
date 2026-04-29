"use client";

import { useMemo } from "react";

import type { Trade } from "@/lib/api";
import { t } from "@/lib/i18n";

type Props = {
  trades: Trade[] | undefined;
  isLoading: boolean;
};

const TZ = "Asia/Taipei";
const DATE_FMT = new Intl.DateTimeFormat("zh-Hant-TW", {
  timeZone: TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function fmtDate(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    // Intl outputs "2026/04/29 10:32" — normalise / → -
    return DATE_FMT.format(d).replace(/\//g, "-");
  } catch {
    return iso;
  }
}

function fmtPrice(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toFixed(1);
}

function fmtHold(entryISO: string, exitISO: string | null): string {
  if (!exitISO) return "—";
  const entry = new Date(entryISO).getTime();
  const exit = new Date(exitISO).getTime();
  if (Number.isNaN(entry) || Number.isNaN(exit) || exit <= entry) return "—";
  const minutes = Math.round((exit - entry) / 60_000);
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
}

function fmtPnl(pnl: number | null): {
  text: string;
  tone: "win" | "loss" | "neutral";
} {
  if (pnl == null) return { text: "—", tone: "neutral" };
  const glyph = pnl > 0 ? "▲" : "▼";
  const sign = pnl > 0 ? "+" : pnl < 0 ? "" : ""; // toFixed already prints "-"
  return {
    text: `${glyph} ${sign}${pnl.toFixed(1)}`,
    tone: pnl > 0 ? "win" : "loss",
  };
}

const SKELETON_ROWS = 8;
const COLS = 6;

export function TradesTable({ trades, isLoading }: Props) {
  const sorted = useMemo(() => {
    if (!trades) return [];
    return [...trades].sort((a, b) => {
      const ta = new Date(a.entry_ts).getTime();
      const tb = new Date(b.entry_ts).getTime();
      return tb - ta;
    });
  }, [trades]);

  return (
    <table className="trades-table" aria-busy={isLoading} aria-live="polite">
      <caption
        style={{
          position: "absolute",
          width: 1,
          height: 1,
          overflow: "hidden",
          clip: "rect(0 0 0 0)",
          whiteSpace: "nowrap",
        }}
      >
        {isLoading ? t("trades.loading") : t("trades.col.date")}
      </caption>
      <thead>
        <tr>
          <th>{t("trades.col.date")}</th>
          <th>{t("trades.col.side")}</th>
          <th style={{ textAlign: "right" }}>{t("trades.col.entry")}</th>
          <th style={{ textAlign: "right" }}>{t("trades.col.exit")}</th>
          <th style={{ textAlign: "right" }}>{t("trades.col.hold")}</th>
          <th style={{ textAlign: "right" }}>
            {t("trades.col.pnl")} ({t("kpi.unit.points")})
          </th>
        </tr>
      </thead>
      <tbody>
        {isLoading &&
          Array.from({ length: SKELETON_ROWS }).map((_, i) => (
            <tr key={`sk-${i}`} className="skeleton-row" aria-hidden="true">
              {Array.from({ length: COLS }).map((__, j) => (
                <td key={j}>
                  <span style={{ visibility: "hidden" }}>—</span>
                </td>
              ))}
            </tr>
          ))}

        {!isLoading && sorted.length === 0 && (
          <tr>
            <td className="empty" colSpan={COLS}>
              {t("trades.empty")}
            </td>
          </tr>
        )}

        {!isLoading &&
          sorted.map((tr) => {
            const isLong = tr.side === "LONG";
            const sideGlyph = isLong ? "▲" : "▼";
            const sideColor = isLong ? "var(--up)" : "var(--down)";
            const sideLabel = isLong ? t("side.long") : t("side.short");
            const pnl = fmtPnl(tr.pnl_points);

            return (
              <tr key={tr.id}>
                <td className="tnum">{fmtDate(tr.entry_ts)}</td>
                <td>
                  <span
                    aria-label={sideLabel}
                    style={{
                      color: sideColor,
                      fontVariantNumeric: "tabular-nums",
                      letterSpacing: "0.04em",
                    }}
                  >
                    <span aria-hidden="true">{sideGlyph}</span> {sideLabel}
                  </span>
                </td>
                <td className="num">{fmtPrice(tr.entry_price)}</td>
                <td className="num">{fmtPrice(tr.exit_price)}</td>
                <td className="num">{fmtHold(tr.entry_ts, tr.exit_ts)}</td>
                <td
                  className={`num${pnl.tone === "win" ? " win" : pnl.tone === "loss" ? " loss" : ""}`}
                >
                  {pnl.text}
                </td>
              </tr>
            );
          })}
      </tbody>
    </table>
  );
}
