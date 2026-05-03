"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, type StrategyOut } from "@/lib/api";

type Props = {
  /** Render the panel with a section title heading. */
  withSectionTitle?: boolean;
};

const STRATEGY_COLOR_VARS = ["--strategy-1", "--strategy-2"];

export function StrategyDescription({ withSectionTitle }: Props) {
  const stratsQ = useQuery({
    queryKey: ["strategies"],
    queryFn: api.strategies,
    refetchInterval: 30_000,
  });

  const items = useMemo(() => {
    const data = stratsQ.data ?? [];
    return data
      .filter((s) => s.spec || s.description)
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [stratsQ.data]);

  return (
    <section
      aria-label="策略說明"
      style={{
        padding: "12px 16px",
        background: "var(--panel-soft, var(--panel))",
        border: "1px solid var(--rule)",
        borderRadius: 8,
        display: "flex",
        flexDirection: "column",
        gap: 16,
      }}
    >
      {withSectionTitle && (
        <h3 className="section-title" style={{ margin: 0 }}>
          策略說明
        </h3>
      )}

      {stratsQ.isLoading || stratsQ.isPending ? (
        <p style={{ margin: 0, color: "var(--ink-muted)" }}>載入中…</p>
      ) : items.length === 0 ? (
        <p style={{ margin: 0, color: "var(--ink-muted)" }}>
          策略尚未提供說明。
        </p>
      ) : (
        items.map((s, i) => (
          <StrategyBlock
            key={s.name}
            entry={s}
            colorVar={STRATEGY_COLOR_VARS[i % STRATEGY_COLOR_VARS.length]}
          />
        ))
      )}
    </section>
  );
}

function StrategyBlock({
  entry,
  colorVar,
}: {
  entry: StrategyOut;
  colorVar: string;
}) {
  const display = entry.display_name ?? entry.name;
  const spec = entry.spec ?? null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <span
          aria-hidden
          style={{
            display: "inline-block",
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: `var(${colorVar})`,
            flexShrink: 0,
          }}
        />
        <span
          style={{
            fontSize: "var(--fs-subhead)",
            fontWeight: "var(--fw-bold)",
            color: "var(--ink)",
          }}
        >
          {display}
        </span>
        <span
          style={{
            fontSize: "var(--fs-caption)",
            color: "var(--ink-muted)",
            fontFamily: "monospace",
          }}
        >
          {entry.name}
        </span>
      </div>

      {spec ? (
        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "max-content 1fr",
            columnGap: 12,
            rowGap: 6,
            margin: 0,
            fontSize: "var(--fs-body)",
            lineHeight: 1.55,
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {Object.entries(spec).map(([label, value]) => (
            <div
              key={label}
              style={{ display: "contents" }}
            >
              <dt
                style={{
                  color: "var(--ink-muted)",
                  fontWeight: "var(--fw-semi)",
                  whiteSpace: "nowrap",
                }}
              >
                {label}
              </dt>
              <dd style={{ margin: 0, color: "var(--ink)" }}>{value}</dd>
            </div>
          ))}
        </dl>
      ) : entry.description ? (
        <p
          style={{
            margin: 0,
            fontSize: "var(--fs-body)",
            lineHeight: 1.55,
            color: "var(--ink)",
          }}
        >
          {entry.description}
        </p>
      ) : null}
    </div>
  );
}
