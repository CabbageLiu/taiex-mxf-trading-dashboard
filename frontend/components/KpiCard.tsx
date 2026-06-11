"use client";

import type { ReactNode } from "react";

import { Skeleton } from "./Skeleton";

export type KpiTone = "neutral" | "up" | "down";

export type KpiCardProps = {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: KpiTone;
  isLoading?: boolean;
};

/**
 * Bullet-chart style KPI card. Number is `tabular-nums` so adjacent
 * cards line up across a row. Tone colours the value only — the
 * label and sub stay in muted ink.
 *
 * Note: per `color-not-only` UX rule the consumer should ensure win/loss
 * carries a glyph as well as a tone — this card doesn't add one
 * automatically because some KPIs (e.g. trade count) are tone-neutral.
 */
export function KpiCard({ label, value, sub, tone = "neutral", isLoading = false }: KpiCardProps) {
  const valueColor =
    tone === "up" ? "var(--up)" : tone === "down" ? "var(--down)" : "var(--ink)";

  return (
    <div className="kpi-card card-enter">
      <div className="label">{label}</div>
      <div className="num" style={{ color: valueColor }}>
        {isLoading ? (
          <Skeleton width={100} height={30} />
        ) : (
          <span className="num" key={String(value)}>{value}</span>
        )}
      </div>
      {sub != null && (
        <div className="delta">
          {isLoading ? <Skeleton width={80} height={11} /> : sub}
        </div>
      )}
    </div>
  );
}
