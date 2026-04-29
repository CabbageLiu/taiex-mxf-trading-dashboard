"use client";

import { useEffect, useRef, useState } from "react";

import { t } from "@/lib/i18n";

export type TradeFilterValue = {
  strategy: string | null;
  start: string | null; // ISO date "YYYY-MM-DD"
  end: string | null;   // ISO date "YYYY-MM-DD"
  result: "all" | "win" | "loss";
};

type Props = {
  filter: TradeFilterValue;
  onChange: (next: TradeFilterValue) => void;
};

const RESULTS: Array<{ key: "all" | "win" | "loss"; label: string }> = [
  { key: "all",  label: t("filter.all") },
  { key: "win",  label: t("filter.win") },
  { key: "loss", label: t("filter.loss") },
];

/**
 * Compact filter row for the analysis page.
 *
 * - Date inputs are debounced 300ms so partial typing in <input type="date">
 *   doesn't fire query refetches per keystroke.
 * - Strategy is shown as a read-only label here. The global StrategySelector
 *   in the shell header (writes `?s=` URL param) is the canonical setter —
 *   duplicating it here would create two sources of truth. (Decision noted
 *   in the agent report.)
 * - Result pills follow `.filter-pill[aria-pressed]` styling already in
 *   globals.css; touch target is 32px min via that class — but per spec we
 *   want ≥44px; we bump min-height inline.
 */
export function TradeFilterBar({ filter, onChange }: Props) {
  // Local controlled-input state so the date input is responsive even while
  // we debounce the upstream onChange call.
  const [start, setStart] = useState(filter.start ?? "");
  const [end, setEnd] = useState(filter.end ?? "");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Resync when parent updates filter (e.g. URL change or initial load)
  useEffect(() => { setStart(filter.start ?? ""); }, [filter.start]);
  useEffect(() => { setEnd(filter.end ?? ""); }, [filter.end]);

  const scheduleEmit = (next: TradeFilterValue) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => onChange(next), 300);
  };

  const onStartChange = (v: string) => {
    setStart(v);
    scheduleEmit({ ...filter, start: v || null });
  };
  const onEndChange = (v: string) => {
    setEnd(v);
    scheduleEmit({ ...filter, end: v || null });
  };

  const setResult = (next: "all" | "win" | "loss") => {
    if (next === filter.result) return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    onChange({ ...filter, result: next });
  };

  return (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: 16,
        padding: "12px 0",
      }}
    >
      <label
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          fontSize: 12,
          color: "var(--ink-muted)",
          letterSpacing: "0.06em",
        }}
      >
        {t("filter.dateRange")}
        <input
          type="date"
          value={start}
          onChange={(e) => onStartChange(e.target.value)}
          aria-label={`${t("filter.dateRange")} - start`}
        />
        <span style={{ color: "var(--ink-muted)" }}>–</span>
        <input
          type="date"
          value={end}
          onChange={(e) => onEndChange(e.target.value)}
          aria-label={`${t("filter.dateRange")} - end`}
        />
      </label>

      <span
        style={{
          fontSize: 12,
          color: "var(--ink-muted)",
          letterSpacing: "0.06em",
        }}
      >
        {t("panel_strategies")}:&nbsp;
        <span style={{ color: "var(--ink)" }}>
          {filter.strategy ?? "—"}
        </span>
      </span>

      <div
        role="radiogroup"
        aria-label="result filter"
        style={{ display: "inline-flex", gap: 8, marginLeft: "auto" }}
      >
        {RESULTS.map((r) => (
          <button
            key={r.key}
            type="button"
            role="radio"
            aria-checked={filter.result === r.key}
            aria-pressed={filter.result === r.key}
            className={`filter-pill${filter.result === r.key ? " active" : ""}`}
            onClick={() => setResult(r.key)}
            style={{ minHeight: 44, padding: "8px 16px" }}
          >
            {r.label}
          </button>
        ))}
      </div>
    </div>
  );
}
