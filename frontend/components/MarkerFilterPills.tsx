"use client";

import { t } from "@/lib/i18n";

/**
 * Strategy descriptor accepted by this toolbar. Local interface so this
 * component does not depend on any extension to `StrategyOut` in `lib/api.ts`
 * (a sibling slice may evolve that type independently).
 */
export type MarkerFilterStrategy = {
  name: string;
  // Accept missing / null / string so this stays compatible with the
  // `StrategyOut.display_name?: string | null` shape returned by `useStrategies`
  // before backend slice A4 redeploys.
  display_name?: string | null;
};

type Props = {
  /**
   * The active strategy-name filter set. `null` means "show all strategies"
   * (the default). A populated `Set` gates marker rendering to those names.
   */
  value: Set<string> | null;
  onChange: (next: Set<string> | null) => void;
  /**
   * Known strategies. May be `undefined` while the upstream query is in
   * flight; in that case we render only the `全部` pill (active).
   */
  strategies?: Array<MarkerFilterStrategy>;
};

/**
 * V5 Phase B Slice B3: filter pills above the chart that gate which
 * strategies' trade markers render. State is local on `/trading` (no URL
 * persistence) and applied chart-side as a final filter on `tradeEvents`.
 *
 * Rendering rules:
 *   * `全部` pill is active when `value === null` (the default cold state).
 *   * One pill per known strategy; pill label uses `display_name ?? name`.
 *     Active when `value?.has(s.name)`. Click → toggle that name in the set.
 *     Toggling the last entry out collapses back to `null` (== "show all"),
 *     which keeps the user from accidentally landing on an empty set that
 *     would hide every marker.
 *   * Reuses the `.indicator-pill` class so the visual language matches the
 *     rest of the toolbar without introducing a new CSS rule.
 */
export function MarkerFilterPills({ value, onChange, strategies }: Props) {
  const list = strategies ?? [];
  const allActive = value === null;

  const toggle = (name: string) => {
    const cur = value ?? new Set<string>();
    const next = new Set(cur);
    if (next.has(name)) {
      next.delete(name);
    } else {
      next.add(name);
    }
    // Empty set === "all" semantically; collapse back to null so the `全部`
    // pill rehighlights and consumers can short-circuit (`!filter`).
    onChange(next.size === 0 ? null : next);
  };

  return (
    <div
      role="group"
      aria-label={t("filter.markers.label")}
      style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}
    >
      <button
        type="button"
        className="indicator-pill"
        aria-pressed={allActive}
        onClick={() => onChange(null)}
      >
        {t("filter.all")}
      </button>
      {list.map((s) => {
        const active = value?.has(s.name) ?? false;
        return (
          <button
            key={s.name}
            type="button"
            className="indicator-pill"
            aria-pressed={active}
            onClick={() => toggle(s.name)}
          >
            {s.display_name ?? s.name}
          </button>
        );
      })}
    </div>
  );
}
