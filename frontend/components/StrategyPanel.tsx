"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type StrategyOut } from "@/lib/api";
import { t } from "@/lib/i18n";

export function StrategyPanel() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["strategies"],
    queryFn: api.strategies,
    refetchInterval: 10_000,
  });

  const toggle = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) => api.enableStrategy(name, enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });

  return (
    <div className="panel">
      <h3>{t("panel_strategies")}</h3>
      {isLoading && <div style={{ color: "var(--muted)" }}>{t("state_loading")}</div>}
      {error && <div className="err">{t("state_failed_prefix")}{(error as Error).message}</div>}
      {(data ?? []).map((s: StrategyOut) => (
        <div key={s.name} className="row">
          <div>
            <div>{s.name}</div>
            <div style={{ color: "var(--muted)", fontSize: 11 }}>
              {s.resolutions.join(", ")} · {t("channels_label")}：{s.channels.join(", ")}
            </div>
          </div>
          <button
            className="btn"
            aria-pressed={s.enabled}
            onClick={() => toggle.mutate({ name: s.name, enabled: !s.enabled })}
          >
            {s.enabled ? t("btn_on") : t("btn_off")}
          </button>
        </div>
      ))}
      {!isLoading && !error && (data ?? []).length === 0 && (
        <div style={{ color: "var(--muted)" }}>{t("state_none_strategies")}</div>
      )}
    </div>
  );
}
