"use client";

import type { IndicatorState } from "./Chart";

type Props = {
  state: IndicatorState;
  onChange: (next: IndicatorState) => void;
};

/**
 * Compact pill row for KD / DMI / MACD / RSI / MA. Active state =
 * sumi-gold filled border. For MA and RSI the pill exposes an inline
 * tabular `period` editor when active.
 *
 * Indicator labels (`MACD`, `KD`, `DMI`, `RSI`, `MA`) stay raw English
 * by design — they are not run through `t()`.
 */
export function IndicatorToggleBar({ state, onChange }: Props) {
  const togglePlain = (key: "macd" | "dmi" | "kd") => {
    onChange({ ...state, [key]: { ...state[key], enabled: !state[key].enabled } });
  };

  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
      <button
        className="indicator-pill"
        aria-pressed={state.macd.enabled}
        onClick={() => togglePlain("macd")}
      >
        MACD
      </button>
      <button
        className="indicator-pill"
        aria-pressed={state.kd.enabled}
        onClick={() => togglePlain("kd")}
      >
        KD
      </button>
      <button
        className="indicator-pill"
        aria-pressed={state.dmi.enabled}
        onClick={() => togglePlain("dmi")}
      >
        DMI
      </button>

      {/* RSI w/ inline period editor */}
      <span
        className="indicator-pill"
        aria-pressed={state.rsi.enabled}
        role="button"
        tabIndex={0}
        onClick={(e) => {
          if ((e.target as HTMLElement).tagName === "INPUT") return;
          onChange({ ...state, rsi: { ...state.rsi, enabled: !state.rsi.enabled } });
        }}
        onKeyDown={(e) => {
          if (e.key === " " || e.key === "Enter") {
            if ((e.target as HTMLElement).tagName === "INPUT") return;
            e.preventDefault();
            onChange({ ...state, rsi: { ...state.rsi, enabled: !state.rsi.enabled } });
          }
        }}
      >
        RSI
        {state.rsi.enabled && (
          <input
            className="period-input"
            type="number"
            min={2}
            max={200}
            value={state.rsi.period}
            onChange={(e) =>
              onChange({ ...state, rsi: { ...state.rsi, period: Number(e.target.value) || 14 } })
            }
            onClick={(e) => e.stopPropagation()}
            aria-label="RSI period"
          />
        )}
      </span>

      {/* MA w/ inline period editor + sma/ema toggle */}
      <span
        className="indicator-pill"
        aria-pressed={state.ma.enabled}
        role="button"
        tabIndex={0}
        onClick={(e) => {
          if ((e.target as HTMLElement).tagName === "INPUT") return;
          if ((e.target as HTMLElement).tagName === "SELECT") return;
          onChange({ ...state, ma: { ...state.ma, enabled: !state.ma.enabled } });
        }}
        onKeyDown={(e) => {
          if (e.key === " " || e.key === "Enter") {
            const tag = (e.target as HTMLElement).tagName;
            if (tag === "INPUT" || tag === "SELECT") return;
            e.preventDefault();
            onChange({ ...state, ma: { ...state.ma, enabled: !state.ma.enabled } });
          }
        }}
      >
        MA
        {state.ma.enabled && (
          <>
            <select
              value={state.ma.kind}
              onChange={(e) =>
                onChange({ ...state, ma: { ...state.ma, kind: e.target.value as "sma" | "ema" } })
              }
              onClick={(e) => e.stopPropagation()}
              style={{
                fontSize: 11, padding: "1px 4px", minHeight: 22,
                background: "var(--panel)", color: "var(--ink)",
              }}
              aria-label="MA kind"
            >
              <option value="sma">SMA</option>
              <option value="ema">EMA</option>
            </select>
            <input
              className="period-input"
              type="number"
              min={2}
              max={400}
              value={state.ma.period}
              onChange={(e) =>
                onChange({ ...state, ma: { ...state.ma, period: Number(e.target.value) || 20 } })
              }
              onClick={(e) => e.stopPropagation()}
              aria-label="MA period"
            />
          </>
        )}
      </span>
    </div>
  );
}
