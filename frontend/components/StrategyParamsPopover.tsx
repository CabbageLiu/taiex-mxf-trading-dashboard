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
      <h4>{strategy.name} · {t("label_period")}</h4>
      {fields.length === 0 && (
        <div style={{ color: "var(--ink-muted)", fontSize: 12 }}>{t("state_none")}</div>
      )}
      {fields.map((f) => (
        <div key={f.key} className="form-row">
          <label htmlFor={`p-${strategy.name}-${f.key}`} style={{ color: "var(--ink-muted)", fontSize: 12 }}>
            {f.key}
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
