"use client";

import { useStatus } from "@/lib/queries";
import { t } from "@/lib/i18n";

type State = "ok" | "lag" | "error";

function deriveState(data: ReturnType<typeof useStatus>["data"], failed: boolean): State {
  if (failed || !data || !data.ok) return "error";
  const lag = data.ingest_lag_seconds;
  if (lag != null && lag > 30) return "lag";
  return "ok";
}

function fmtLag(sec: number | null | undefined): string {
  if (sec == null) return "—";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  if (sec < 3600) return `${(sec / 60).toFixed(1)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("zh-Hant-TW", { timeZone: "Asia/Taipei", hour12: false });
  } catch {
    return iso;
  }
}

function notifierLabel(map: { discord: boolean; n8n: boolean; inapp: boolean }): string {
  const on = Object.entries(map).filter(([, v]) => v).map(([k]) => k);
  return on.length ? on.join(", ") : "—";
}

export function StatusPill() {
  const { data, isError } = useStatus();
  const state = deriveState(data, isError);
  const label = state === "ok" ? t("status.ok") : state === "lag" ? t("status.lag") : t("status.error");

  return (
    <span className="status-pill" tabIndex={0} aria-label={label}>
      <span className="status-dot" data-state={state} />
      <span>{label}</span>
      <span className="tip" role="tooltip">
        <dl>
          <dt>{t("status.lastTick")}</dt>
          <dd>{fmtTs(data?.last_tick_ts)}</dd>
          <dt>{t("status.lagSec")}</dt>
          <dd>{fmtLag(data?.ingest_lag_seconds)}</dd>
          <dt>{t("status.db")}</dt>
          <dd>{data?.db_ok ? "ok" : "down"}</dd>
          <dt>{t("status.notifiers")}</dt>
          <dd>{data ? notifierLabel(data.notifiers) : "—"}</dd>
        </dl>
      </span>
    </span>
  );
}
