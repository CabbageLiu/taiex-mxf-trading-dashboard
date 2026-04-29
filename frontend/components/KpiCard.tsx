"use client";

import type { ReactNode } from "react";

export type KpiTone = "neutral" | "up" | "down";

export type KpiCardProps = {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: KpiTone;
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
export function KpiCard({ label, value, sub, tone = "neutral" }: KpiCardProps) {
  const valueColor =
    tone === "up" ? "var(--up)" : tone === "down" ? "var(--down)" : "var(--ink)";

  return (
    <div className="kpi-card">
      <div className="label">{label}</div>
      <div className="num" style={{ color: valueColor }}>
        {value}
      </div>
      {sub != null && <div className="delta">{sub}</div>}
    </div>
  );
}
