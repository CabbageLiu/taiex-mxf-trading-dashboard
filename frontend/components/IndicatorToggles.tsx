"use client";

import type { IndicatorState } from "./Chart";
import { t } from "@/lib/i18n";

type Props = {
  state: IndicatorState;
  onChange: (next: IndicatorState) => void;
};

export function IndicatorToggles({ state, onChange }: Props) {
  const toggle = (key: keyof IndicatorState) => {
    onChange({ ...state, [key]: { ...state[key], enabled: !state[key].enabled } });
  };

  return (
    <div className="panel">
      <h3>{t("panel_indicators")}</h3>

      <div className="row">
        <button className="btn" aria-pressed={state.macd.enabled} onClick={() => toggle("macd")}>MACD</button>
        <button className="btn" aria-pressed={state.dmi.enabled} onClick={() => toggle("dmi")}>DMI</button>
        <button className="btn" aria-pressed={state.kd.enabled} onClick={() => toggle("kd")}>KD</button>
        <button className="btn" aria-pressed={state.rsi.enabled} onClick={() => toggle("rsi")}>RSI</button>
      </div>

      <div className="row" style={{ marginTop: 8 }}>
        <button className="btn" aria-pressed={state.ma.enabled} onClick={() => toggle("ma")}>MA</button>
        <select
          value={state.ma.kind}
          onChange={(e) => onChange({ ...state, ma: { ...state.ma, kind: e.target.value as "sma" | "ema" } })}
        >
          <option value="sma">SMA</option>
          <option value="ema">EMA</option>
        </select>
        <input
          type="number"
          min={2}
          max={400}
          value={state.ma.period}
          onChange={(e) => onChange({ ...state, ma: { ...state.ma, period: Number(e.target.value) || 20 } })}
          style={{ width: 60 }}
          aria-label={t("label_period")}
        />
      </div>

      <div className="row" style={{ marginTop: 8 }}>
        <span style={{ color: "var(--muted)" }}>{t("label_rsi_period")}</span>
        <input
          type="number"
          min={2}
          max={200}
          value={state.rsi.period}
          onChange={(e) => onChange({ ...state, rsi: { ...state.rsi, period: Number(e.target.value) || 14 } })}
          style={{ width: 60 }}
        />
      </div>
    </div>
  );
}
