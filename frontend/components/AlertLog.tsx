"use client";

import { useMemo } from "react";

import { useQuery } from "@tanstack/react-query";

import { api, type AlertOut } from "@/lib/api";
import {
  useAlertStats,
  useSignals,
  useStatus,
  useStrategies,
  useTestWebhook,
} from "@/lib/queries";
import { t, tSide } from "@/lib/i18n";

// Live-signal-row shape used by trading/page.tsx via the WS path. `id` is
// optional because WS-pushed signals carry the upstream `signals.id` while
// older code paths may not — we dedupe on `id` first then on a composite
// `ts:strategy:side` fallback.
export type SignalRow = {
  id?: number | null;
  ts: string;
  symbol: string;
  resolution: string;
  strategy: string;
  side: string;
  price: number;
  reason: string;
};

type ChannelKey = "discord" | "n8n" | "inapp";

const CHANNELS: Array<{ key: ChannelKey; label: string }> = [
  { key: "discord", label: "Discord" },
  { key: "n8n", label: "n8n" },
  { key: "inapp", label: "In-app" },
];

type Tone = "ok" | "warn" | "off";

function HealthDot({ tone }: { tone: Tone }) {
  const color =
    tone === "ok"
      ? "var(--down)"
      : tone === "warn"
        ? "var(--warn)"
        : "var(--ink-muted, var(--muted))";
  return (
    <span
      aria-hidden
      style={{
        display: "inline-block",
        width: 8,
        height: 8,
        borderRadius: "50%",
        background: color,
        marginRight: 6,
      }}
    />
  );
}

function pickTone(
  stats: { sent: number; failed: number; last_ts: string | null } | undefined,
  configured: boolean,
): Tone {
  if (!configured) return "off";
  if (!stats || (stats.sent === 0 && stats.failed === 0)) return "off";
  if (stats.last_ts) {
    const ageMs = Date.now() - new Date(stats.last_ts).getTime();
    if (ageMs < 24 * 3600 * 1000 && stats.failed <= stats.sent) return "ok";
  }
  return stats.failed > 0 ? "warn" : "off";
}

function dedupKey(s: {
  id?: number | null;
  ts: string;
  strategy: string;
  side: string;
}): string {
  return s.id != null ? `id:${s.id}` : `${s.ts}:${s.strategy}:${s.side}`;
}

export function AlertLog({ liveSignals }: { liveSignals: SignalRow[] }) {
  // 即時訊號 — mount seed via /signals + WS-pushed merge. WS rows take
  // precedence (they carry the most recent state), then we backfill from
  // the seed query up to a 50-row cap.
  const seedQ = useSignals({ limit: 50 });

  // V5 Phase C — map canonical strategy `name` → `display_name` for label
  // rendering on signal rows. The `useStrategies` cache is shared with the
  // selector / marker pills, so this is a free read in practice.
  const strategiesQ = useStrategies();
  const displayNameOf = useMemo(() => {
    const map = new Map<string, string>();
    for (const s of strategiesQ.data ?? []) {
      if (s.display_name) map.set(s.name, s.display_name);
    }
    return (name: string) => map.get(name) ?? name;
  }, [strategiesQ.data]);

  const merged = useMemo<SignalRow[]>(() => {
    const out: SignalRow[] = [];
    const seen = new Set<string>();
    // liveSignals is appended-in-time; reverse so newest-first.
    for (let i = liveSignals.length - 1; i >= 0; i--) {
      const s = liveSignals[i];
      const k = dedupKey(s);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(s);
      if (out.length >= 50) break;
    }
    if (out.length < 50 && seedQ.data) {
      for (const s of seedQ.data) {
        const k = dedupKey(s);
        if (seen.has(k)) continue;
        seen.add(k);
        const reasonRaw = (s.payload as Record<string, unknown> | undefined)?.[
          "reason"
        ];
        out.push({
          id: s.id,
          ts: s.ts,
          symbol: s.symbol,
          resolution: s.resolution,
          strategy: s.strategy,
          side: s.side,
          price: s.price ?? 0,
          reason: typeof reasonRaw === "string" ? reasonRaw : "",
        });
        if (out.length >= 50) break;
      }
    }
    return out;
  }, [liveSignals, seedQ.data]);

  const onRowClick = (ts: string) => {
    const time = Math.floor(new Date(ts).getTime() / 1000);
    if (typeof window === "undefined" || !Number.isFinite(time)) return;
    window.dispatchEvent(
      new CustomEvent("chart-scroll-to", { detail: { time } }),
    );
  };

  // 通知遞送 — channel chips + recent attempts list.
  const alertsQ = useQuery({
    queryKey: ["alerts"],
    queryFn: () => api.alerts(50),
    refetchInterval: 5_000,
  });
  const statsQ = useAlertStats();
  const statusQ = useStatus();
  const testWebhook = useTestWebhook();

  return (
    <>
      <div className="panel alerts">
        <h3 className="section-title">{t("panel_live_signals")}</h3>
        {merged.length === 0 && (
          <div style={{ color: "var(--muted)" }}>
            {t("alerts.none_signals")}
          </div>
        )}
        {merged.map((s, i) => (
          <button
            key={`${s.id ?? "x"}-${i}-${s.ts}`}
            type="button"
            className="row"
            onClick={() => onRowClick(s.ts)}
            title={s.reason}
            style={{
              all: "unset",
              display: "flex",
              justifyContent: "space-between",
              padding: "4px 0",
              cursor: "pointer",
              width: "100%",
            }}
          >
            <div>
              <span className={`tag ${s.side.toLowerCase()}`}>
                {tSide(s.side)}
              </span>{" "}
              <strong title={s.strategy}>{displayNameOf(s.strategy)}</strong>{" "}
              <span style={{ color: "var(--muted)" }}>{s.resolution}</span>
            </div>
            <div style={{ fontVariantNumeric: "tabular-nums" }}>
              {typeof s.price === "number" && Number.isFinite(s.price)
                ? s.price.toFixed(2)
                : "-"}
            </div>
          </button>
        ))}
      </div>

      <div className="panel alerts">
        <h3 className="section-title">{t("panel_alert_delivery")}</h3>
        <div
          style={{
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            marginBottom: 8,
          }}
        >
          {CHANNELS.map((ch) => {
            const configured = statusQ.data?.notifiers?.[ch.key] ?? false;
            const stats = statsQ.data?.[ch.key];
            const tone = pickTone(stats, configured);
            const sending =
              testWebhook.isPending &&
              testWebhook.variables?.channel === ch.key;
            return (
              <div
                key={ch.key}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "4px 8px",
                  border: "1px solid var(--rule)",
                  borderRadius: 6,
                  fontSize: "var(--fs-caption)",
                }}
                title={
                  stats?.last_ts
                    ? `last ${new Date(stats.last_ts).toLocaleString(
                        "zh-Hant-TW",
                        { timeZone: "Asia/Taipei", hour12: false },
                      )} · sent ${stats.sent} / failed ${stats.failed}`
                    : configured
                      ? "configured"
                      : "not configured"
                }
              >
                <HealthDot tone={tone} />
                <span>{ch.label}</span>
                {configured && (
                  <button
                    type="button"
                    className="btn-text"
                    style={{
                      minHeight: 24,
                      padding: "2px 6px",
                      fontSize: "var(--fs-caption)",
                    }}
                    onClick={() => testWebhook.mutate({ channel: ch.key })}
                    disabled={sending}
                  >
                    {sending ? "…" : t("alerts.test_send")}
                  </button>
                )}
              </div>
            );
          })}
        </div>
        {(alertsQ.data ?? []).slice(0, 20).map((a: AlertOut) => (
          <div key={a.id} className="row">
            <div>
              <span className="tag">{a.channel}</span>{" "}
              <span className={a.status === "ok" ? "ok" : "err"}>
                {a.status}
              </span>
              {a.http_code != null && (
                <span style={{ color: "var(--muted)" }}> ({a.http_code})</span>
              )}
            </div>
            <div
              style={{ color: "var(--muted)", fontSize: "var(--fs-meta)" }}
            >
              {new Date(a.ts).toLocaleTimeString("zh-Hant-TW", {
                timeZone: "Asia/Taipei",
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
                hour12: false,
              })}
            </div>
          </div>
        ))}
        {(alertsQ.data ?? []).length === 0 && (
          <div style={{ color: "var(--muted)" }}>
            {t("alerts.none_alerts")}
          </div>
        )}
      </div>
    </>
  );
}
