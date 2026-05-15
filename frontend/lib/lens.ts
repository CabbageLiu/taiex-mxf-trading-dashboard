"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef } from "react";

import type { IndicatorState } from "@/components/Chart";
import type { Resolution } from "@/components/ResolutionSelector";

// Re-export the underlying types so consumers can `import { Resolution } from "@/lib/lens"`.
export type { IndicatorState } from "@/components/Chart";
export type { Resolution } from "@/components/ResolutionSelector";

/**
 * Default indicator state — must match the literal previously inlined in
 * `app/trading/page.tsx`. Slice C will replace those inline copies with this
 * import.
 */
export const DEFAULT_INDICATORS: IndicatorState = {
  ma: { enabled: true, period: 20, kind: "sma" },
  macd: { enabled: true },
  rsi: { enabled: false, period: 14 },
  kd: { enabled: false },
  dmi: { enabled: false },
};

const DEFAULT_RESOLUTION: Resolution = "1m";

const VALID_RESOLUTIONS: ReadonlyArray<Resolution> = [
  "1m",
  "5m",
  "15m",
  "30m",
  "1h",
  "4h",
  "12h",
  "1d",
  "1w",
  "1mo",
];

const STORAGE_KEY = "taiex.lens.v1";

const LENS_KEYS = ["s", "s2", "start", "end", "res", "ind", "compare"] as const;
type LensKey = (typeof LENS_KEYS)[number];

export type LensState = {
  strategy: string | null;
  secondaryStrategy: string | null;
  start: string | null;
  end: string | null;
  resolution: Resolution;
  indicators: IndicatorState;
  compare: boolean;
};

export type UseLensReturn = LensState & {
  isActive: boolean;
  setStrategy: (s: string | null) => void;
  setSecondaryStrategy: (s: string | null) => void;
  setRange: (start: string | null, end: string | null) => void;
  setResolution: (r: Resolution) => void;
  setIndicators: (ind: IndicatorState) => void;
  setCompare: (c: boolean) => void;
  reset: () => void;
};

// ---------------------------------------------------------------------------
// Indicator codec
// ---------------------------------------------------------------------------

/**
 * Encode an `IndicatorState` into a compact comma-separated form suitable for
 * a URL query value. Each indicator emits a token iff its current state
 * differs from the default. Tokens carry a `+` (enabled) or `-` (disabled)
 * marker so a serialize-then-parse roundtrip is lossless even for disabled
 * indicators with custom periods/kinds.
 *
 * @example
 * encodeIndicators(DEFAULT_INDICATORS);
 * // => ""  (empty — defaults need no token)
 *
 * @example
 * encodeIndicators({
 *   ma:   { enabled: true,  period: 50, kind: "ema" },
 *   macd: { enabled: false },                          // MACD default is on; flip to off
 *   rsi:  { enabled: false, period: 42 },              // disabled but custom period
 *   kd:   { enabled: true },
 *   dmi:  { enabled: true },
 * });
 * // => "ma+:ema:50,macd-,rsi-:42,kd+,dmi+"
 */
export function encodeIndicators(state: IndicatorState): string {
  const tokens: string[] = [];
  const def = DEFAULT_INDICATORS;
  if (
    state.ma.enabled !== def.ma.enabled ||
    state.ma.period !== def.ma.period ||
    state.ma.kind !== def.ma.kind
  ) {
    tokens.push(`ma${state.ma.enabled ? "+" : "-"}:${state.ma.kind}:${state.ma.period}`);
  }
  if (state.macd.enabled !== def.macd.enabled) {
    tokens.push(`macd${state.macd.enabled ? "+" : "-"}`);
  }
  if (
    state.rsi.enabled !== def.rsi.enabled ||
    state.rsi.period !== def.rsi.period
  ) {
    tokens.push(`rsi${state.rsi.enabled ? "+" : "-"}:${state.rsi.period}`);
  }
  if (state.kd.enabled !== def.kd.enabled) {
    tokens.push(`kd${state.kd.enabled ? "+" : "-"}`);
  }
  if (state.dmi.enabled !== def.dmi.enabled) {
    tokens.push(`dmi${state.dmi.enabled ? "+" : "-"}`);
  }
  return tokens.join(",");
}

/**
 * Decode an indicator codec string back into `IndicatorState`. Unknown or
 * malformed tokens are silently ignored — the corresponding indicator stays
 * at its default.
 *
 * @example
 * decodeIndicators(null);            // => DEFAULT_INDICATORS (deep clone)
 * decodeIndicators("");               // => DEFAULT_INDICATORS (deep clone)
 * decodeIndicators("ma:ema:50,rsi:14,kd,dmi");
 * // => {
 * //   ma:   { enabled: true,  period: 50, kind: "ema" },
 * //   macd: { enabled: false },
 * //   rsi:  { enabled: true,  period: 14 },
 * //   kd:   { enabled: true },
 * //   dmi:  { enabled: true },
 * // }
 */
export function decodeIndicators(s: string | null): IndicatorState {
  // Deep clone of defaults — keeps the original constant immutable. Any
  // indicator NOT mentioned in the codec stays at its default.
  const out: IndicatorState = {
    ma: { ...DEFAULT_INDICATORS.ma },
    macd: { ...DEFAULT_INDICATORS.macd },
    rsi: { ...DEFAULT_INDICATORS.rsi },
    kd: { ...DEFAULT_INDICATORS.kd },
    dmi: { ...DEFAULT_INDICATORS.dmi },
  };
  if (!s) return out;

  for (const raw of s.split(",")) {
    const token = raw.trim();
    if (!token) continue;
    const parts = token.split(":");
    const head = parts[0];
    // Strip trailing +/- marker; legacy tokens without a marker are treated
    // as enabled (matches V4 phase 2's original lossy codec for back-compat).
    let key = head;
    let enabled = true;
    if (head.endsWith("+")) {
      key = head.slice(0, -1);
      enabled = true;
    } else if (head.endsWith("-")) {
      key = head.slice(0, -1);
      enabled = false;
    }
    if (key === "ma") {
      const kind = parts[1];
      const period = Number(parts[2]);
      if ((kind === "sma" || kind === "ema") && Number.isFinite(period) && period > 0) {
        out.ma = { enabled, period, kind };
      }
    } else if (key === "macd") {
      out.macd = { enabled };
    } else if (key === "rsi") {
      const period = Number(parts[1]);
      if (Number.isFinite(period) && period > 0) {
        out.rsi = { enabled, period };
      }
    } else if (key === "kd") {
      out.kd = { enabled };
    } else if (key === "dmi") {
      out.dmi = { enabled };
    }
    // unknown token → ignored, indicator stays at default
  }

  return out;
}

// ---------------------------------------------------------------------------
// Resolution helpers
// ---------------------------------------------------------------------------

function parseResolution(raw: string | null): Resolution {
  if (raw && (VALID_RESOLUTIONS as ReadonlyArray<string>).includes(raw)) {
    return raw as Resolution;
  }
  return DEFAULT_RESOLUTION;
}

// ---------------------------------------------------------------------------
// Storage helpers
// ---------------------------------------------------------------------------

type StoredLens = {
  strategy?: string | null;
  secondaryStrategy?: string | null;
  start?: string | null;
  end?: string | null;
  resolution?: string | null;
  indicators?: IndicatorState | null;
  compare?: boolean | null;
};

function readStorage(): StoredLens | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object") return parsed as StoredLens;
  } catch {
    // malformed JSON / disabled storage — ignore
  }
  return null;
}

function writeStorage(state: LensState): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // quota / disabled storage — ignore
  }
}

function clearStorage(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Param helpers
// ---------------------------------------------------------------------------

function hasAnyLensKey(sp: URLSearchParams): boolean {
  for (const key of LENS_KEYS) {
    if (sp.has(key)) return true;
  }
  return false;
}

function buildParamsFromStored(stored: StoredLens): URLSearchParams {
  const params = new URLSearchParams();
  if (stored.strategy) params.set("s", stored.strategy);
  if (stored.secondaryStrategy) params.set("s2", stored.secondaryStrategy);
  if (stored.start) params.set("start", stored.start);
  if (stored.end) params.set("end", stored.end);
  if (stored.resolution && stored.resolution !== DEFAULT_RESOLUTION) {
    params.set("res", stored.resolution);
  }
  if (stored.indicators) {
    const encoded = encodeIndicators(stored.indicators);
    const defaultEncoded = encodeIndicators(DEFAULT_INDICATORS);
    if (encoded !== defaultEncoded) params.set("ind", encoded);
  }
  if (stored.compare) params.set("compare", "1");
  return params;
}

function setOrDelete(params: URLSearchParams, key: LensKey, value: string | null): void {
  if (value == null || value === "") {
    params.delete(key);
  } else {
    params.set(key, value);
  }
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useLens(): UseLensReturn {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Derived current state — recomputed when URL changes.
  const state = useMemo<LensState>(() => {
    const indParam = searchParams.get("ind");
    return {
      strategy: searchParams.get("s"),
      secondaryStrategy: searchParams.get("s2"),
      start: searchParams.get("start"),
      end: searchParams.get("end"),
      resolution: parseResolution(searchParams.get("res")),
      indicators: decodeIndicators(indParam),
      compare: searchParams.get("compare") === "1",
    };
  }, [searchParams]);

  // Hydration-from-localStorage guard — runs once per mount.
  const hydratedRef = useRef(false);
  useEffect(() => {
    if (hydratedRef.current) return;
    hydratedRef.current = true;
    if (typeof window === "undefined") return;

    const sp = new URLSearchParams(searchParams.toString());
    if (hasAnyLensKey(sp)) return;

    const stored = readStorage();
    if (!stored) return;

    const seeded = buildParamsFromStored(stored);
    if (Array.from(seeded.keys()).length === 0) return;

    const qs = seeded.toString();
    router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
    // Intentionally only run on mount — `searchParams` / `pathname` capture
    // the initial values via closure; subsequent URL edits go through setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Generic write helper — copies current params, applies a mutator, then
  // writes to URL + localStorage atomically.
  const writeParams = useCallback(
    (mutate: (params: URLSearchParams) => void) => {
      const params = new URLSearchParams(searchParams.toString());
      mutate(params);

      // Compute the resulting LensState from the mutated params and persist
      // it. `searchParams` won't reflect the mutation until React re-renders,
      // so we re-derive from `params` directly.
      const indParam = params.get("ind");
      const next: LensState = {
        strategy: params.get("s"),
        secondaryStrategy: params.get("s2"),
        start: params.get("start"),
        end: params.get("end"),
        resolution: parseResolution(params.get("res")),
        indicators: decodeIndicators(indParam),
        compare: params.get("compare") === "1",
      };
      writeStorage(next);

      const qs = params.toString();
      router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
    },
    [pathname, router, searchParams],
  );

  const setStrategy = useCallback(
    (s: string | null) => {
      writeParams((p) => setOrDelete(p, "s", s));
    },
    [writeParams],
  );

  const setSecondaryStrategy = useCallback(
    (s: string | null) => {
      writeParams((p) => setOrDelete(p, "s2", s));
    },
    [writeParams],
  );

  const setRange = useCallback(
    (start: string | null, end: string | null) => {
      writeParams((p) => {
        setOrDelete(p, "start", start);
        setOrDelete(p, "end", end);
      });
    },
    [writeParams],
  );

  const setResolution = useCallback(
    (r: Resolution) => {
      writeParams((p) => {
        if (r === DEFAULT_RESOLUTION) p.delete("res");
        else p.set("res", r);
      });
    },
    [writeParams],
  );

  const setIndicators = useCallback(
    (ind: IndicatorState) => {
      const encoded = encodeIndicators(ind);
      const defaultEncoded = encodeIndicators(DEFAULT_INDICATORS);
      writeParams((p) => {
        if (encoded === defaultEncoded) p.delete("ind");
        else p.set("ind", encoded);
      });
    },
    [writeParams],
  );

  const setCompare = useCallback(
    (c: boolean) => {
      writeParams((p) => {
        if (c) p.set("compare", "1");
        else p.delete("compare");
      });
    },
    [writeParams],
  );

  const reset = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString());
    for (const key of LENS_KEYS) params.delete(key);
    clearStorage();
    const qs = params.toString();
    router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
  }, [pathname, router, searchParams]);

  return {
    ...state,
    isActive: state.strategy != null,
    setStrategy,
    setSecondaryStrategy,
    setRange,
    setResolution,
    setIndicators,
    setCompare,
    reset,
  };
}
