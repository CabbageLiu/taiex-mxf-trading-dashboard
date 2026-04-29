"use client";

import { Suspense } from "react";

import type { IndicatorState } from "./Chart";
import { IndicatorToggleBar } from "./IndicatorToggleBar";
import { ResolutionSelector, type Resolution } from "./ResolutionSelector";
import { StrategySelector } from "./StrategySelector";

type Props = {
  resolution: Resolution;
  onResolutionChange: (r: Resolution) => void;
  indicators: IndicatorState;
  onIndicatorsChange: (next: IndicatorState) => void;
};

/**
 * Horizontal panel-styled toolbar shared by the trading view.
 * Composes resolution selector + strategy combobox + indicator toggle pills.
 */
export function TopBar({ resolution, onResolutionChange, indicators, onIndicatorsChange }: Props) {
  return (
    <div className="topbar" role="toolbar" aria-label="Trading toolbar">
      <div className="group">
        <ResolutionSelector value={resolution} onChange={onResolutionChange} />
      </div>
      <div className="group">
        <Suspense fallback={<div style={{ minWidth: 220 }} />}>
          <StrategySelector />
        </Suspense>
      </div>
      <div className="group">
        <IndicatorToggleBar state={indicators} onChange={onIndicatorsChange} />
      </div>
    </div>
  );
}
