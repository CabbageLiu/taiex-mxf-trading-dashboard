"use client";

import { useQuery } from "@tanstack/react-query";
import { api, type AlertOut } from "@/lib/api";
import type { WsMessage } from "@/lib/ws";
import { useMemo } from "react";
import { t, tSide } from "@/lib/i18n";

export type SignalRow = {
  ts: string;
  symbol: string;
  resolution: string;
  strategy: string;
  side: string;
  price: number;
  reason: string;
};

export function AlertLog({ liveSignals }: { liveSignals: SignalRow[] }) {
  const { data: alerts } = useQuery({
    queryKey: ["alerts"],
    queryFn: () => api.alerts(50),
    refetchInterval: 5_000,
  });

  const merged = useMemo(() => {
    return [...liveSignals].slice(-30).reverse();
  }, [liveSignals]);

  return (
    <>
      <div className="panel alerts">
        <h3 className="section-title">{t("panel_live_signals")}</h3>
        {merged.length === 0 && <div style={{ color: "var(--muted)" }}>{t("state_none")}</div>}
        {merged.map((s, i) => (
          <div key={i} className="row">
            <div>
              <span className={`tag ${s.side.toLowerCase()}`}>{tSide(s.side)}</span>{" "}
              <strong>{s.strategy}</strong>{" "}
              <span style={{ color: "var(--muted)" }}>{s.resolution}</span>
            </div>
            <div>{s.price?.toFixed?.(2) ?? "-"}</div>
          </div>
        ))}
      </div>
      <div className="panel alerts">
        <h3 className="section-title">{t("panel_alert_delivery")}</h3>
        {(alerts ?? []).slice(0, 20).map((a: AlertOut) => (
          <div key={a.id} className="row">
            <div>
              <span className="tag">{a.channel}</span>{" "}
              <span className={a.status === "ok" ? "ok" : "err"}>{a.status}</span>
              {a.http_code != null && <span style={{ color: "var(--muted)" }}> ({a.http_code})</span>}
            </div>
            <div style={{ color: "var(--muted)", fontSize: "var(--fs-meta)" }}>
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
        {(alerts ?? []).length === 0 && <div style={{ color: "var(--muted)" }}>{t("state_none")}</div>}
      </div>
    </>
  );
}
