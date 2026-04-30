"use client";

import { RefreshCw } from "lucide-react";
import { Suspense, useEffect, useRef, useState } from "react";

import type { IndicatorState } from "./Chart";
import { IndicatorToggleBar } from "./IndicatorToggleBar";
import {
  MarkerFilterPills,
  type MarkerFilterStrategy,
} from "./MarkerFilterPills";
import { ResolutionSelector, type Resolution } from "./ResolutionSelector";
import { StrategySelector } from "./StrategySelector";

type Props = {
  resolution: Resolution;
  onResolutionChange: (r: Resolution) => void;
  indicators: IndicatorState;
  onIndicatorsChange: (next: IndicatorState) => void;
  onRefresh: () => void;
  isRefreshing: boolean;
  // V5 Phase B Slice B3 — chart marker filter. `null` means show all.
  markerFilter: Set<string> | null;
  onMarkerFilterChange: (next: Set<string> | null) => void;
  strategies: Array<MarkerFilterStrategy> | undefined;
};

/**
 * Horizontal panel-styled toolbar shared by the trading view.
 * Composes resolution selector + strategy combobox + indicator toggle pills.
 */
export function TopBar({
  resolution,
  onResolutionChange,
  indicators,
  onIndicatorsChange,
  onRefresh,
  isRefreshing,
  markerFilter,
  onMarkerFilterChange,
  strategies,
}: Props) {
  // Pulse the refresh button briefly when an in-flight refresh resolves.
  // Hits the keyframe `pulseAccent` once via the `pulse-success` class.
  const [pulse, setPulse] = useState(false);
  const prevRefreshing = useRef(isRefreshing);
  useEffect(() => {
    if (prevRefreshing.current && !isRefreshing) {
      setPulse(true);
      const t = setTimeout(() => setPulse(false), 1200);
      prevRefreshing.current = isRefreshing;
      return () => clearTimeout(t);
    }
    prevRefreshing.current = isRefreshing;
    return undefined;
  }, [isRefreshing]);

  const refreshClass = [
    "icon-btn",
    "refresh-btn",
    pulse ? "pulse-success" : "",
  ]
    .filter(Boolean)
    .join(" ");

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
      <div className="group">
        <MarkerFilterPills
          value={markerFilter}
          onChange={onMarkerFilterChange}
          strategies={strategies}
        />
      </div>
      <div className="group">
        <button
          type="button"
          className={refreshClass}
          onClick={onRefresh}
          disabled={isRefreshing}
          aria-label="重新整理 K 線"
          title="重新整理 K 線"
        >
          <RefreshCw size={16} className={isRefreshing ? "spinning" : undefined} aria-hidden />
        </button>
      </div>
    </div>
  );
}
