"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, type StrategyOut } from "@/lib/api";
import { t } from "@/lib/i18n";

type Props = {
  strategy: StrategyOut;
  onClose: () => void;
};

type FieldType = "string" | "number" | "integer" | "boolean";

type FieldSpec = {
  key: string;
  type: FieldType;
  default?: unknown;
};

/**
 * Coerce a JSON-Schema-ish `params_schema` produced by Pydantic v2
 * into a flat list of fields the popover can render. Falls back to the
 * existing param keys when the schema is sparse.
 */
// Traditional Chinese labels for known parameter keys. Technical-indicator
// names (DI, MACD, KD, RSI, MA) stay in English by design — they're the
// universal reference. Unknown keys fall through to the raw key as a
// last-resort label so newly-added params still render something.
const PARAM_LABELS: Record<string, string> = {
  enable_short: "啟用做空",
  kd_period: "KD 週期",
  kd_long_floor: "KD 做多門檻",
  kd_short_ceiling: "KD 做空門檻",
  macd_fast: "MACD 快線週期",
  macd_slow: "MACD 慢線週期",
  macd_signal: "MACD 訊號線週期",
  dmi_period: "DMI 週期",
  di_long_threshold: "+DI 做多門檻",
  di_short_threshold: "−DI 做空門檻",
  exit_di_threshold: "DI 離場門檻",
  tp_points: "停利點數",
  sl_points: "停損點數",
  cooldown_bars: "出場後冷卻 K 棒數",
};

function paramLabel(key: string): string {
  return PARAM_LABELS[key] ?? key;
}

function buildFields(strategy: StrategyOut): FieldSpec[] {
  const schema = strategy.params_schema ?? {};
  const props: Record<string, any> = schema.properties ?? {};
  const knownKeys = new Set([...Object.keys(props), ...Object.keys(strategy.params ?? {})]);
  const out: FieldSpec[] = [];
  for (const key of knownKeys) {
    const node = props[key] ?? {};
    let type: FieldType = "string";
    const t = Array.isArray(node.type) ? node.type[0] : node.type;
    if (t === "integer") type = "integer";
    else if (t === "number") type = "number";
    else if (t === "boolean") type = "boolean";
    else if (t === "string") type = "string";
    else if (typeof strategy.params?.[key] === "number") type = "number";
    else if (typeof strategy.params?.[key] === "boolean") type = "boolean";
    out.push({ key, type, default: node.default });
  }
  out.sort((a, b) => a.key.localeCompare(b.key));
  return out;
}

function coerce(value: string, type: FieldType): unknown {
  if (type === "integer") {
    const n = parseInt(value, 10);
    return Number.isFinite(n) ? n : 0;
  }
  if (type === "number") {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }
  if (type === "boolean") return value === "true";
  return value;
}

export function StrategyParamsPopover({ strategy, onClose }: Props) {
  const fields = useMemo(() => buildFields(strategy), [strategy]);
  const initial = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const f of fields) {
      const cur = (strategy.params ?? {})[f.key];
      out[f.key] = cur ?? f.default ?? (f.type === "boolean" ? false : f.type === "string" ? "" : 0);
    }
    return out;
  }, [fields, strategy]);

  const [values, setValues] = useState<Record<string, unknown>>(initial);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement | null>(null);
  const qc = useQueryClient();

  useEffect(() => {
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    function onClick(e: MouseEvent) {
      if (!ref.current) return;
      if (!ref.current.contains(e.target as Node)) onClose();
    }
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [onClose]);

  const save = useMutation({
    mutationFn: () => api.setStrategyParams(strategy.name, { params: values }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategies"] });
      onClose();
    },
    onError: (e: Error) => setError(e.message),
  });

  return (
    <div className="popover" ref={ref} role="dialog" aria-label={`${strategy.name} params`}>
      <h4>{strategy.name} · 參數設定</h4>
      {fields.length === 0 && (
        <div style={{ color: "var(--ink-muted)", fontSize: 12 }}>{t("state_none")}</div>
      )}
      {fields.map((f) => (
        <div key={f.key} className="form-row">
          <label
            htmlFor={`p-${strategy.name}-${f.key}`}
            title={f.key}
            style={{ color: "var(--ink-muted)", fontSize: 12 }}
          >
            {paramLabel(f.key)}
          </label>
          {f.type === "boolean" ? (
            <input
              id={`p-${strategy.name}-${f.key}`}
              type="checkbox"
              checked={Boolean(values[f.key])}
              onChange={(e) => setValues({ ...values, [f.key]: e.target.checked })}
            />
          ) : (
            <input
              id={`p-${strategy.name}-${f.key}`}
              type={f.type === "string" ? "text" : "number"}
              value={String(values[f.key] ?? "")}
              step={f.type === "integer" ? 1 : "any"}
              onChange={(e) => setValues({ ...values, [f.key]: coerce(e.target.value, f.type) })}
            />
          )}
        </div>
      ))}
      {error && <div style={{ color: "var(--up)", fontSize: 12 }}>{error}</div>}
      <div className="actions">
        <button className="btn" onClick={onClose} disabled={save.isPending}>{t("btn_cancel")}</button>
        <button className="btn" onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending ? t("state_loading") : t("btn_save")}
        </button>
      </div>
    </div>
  );
}
