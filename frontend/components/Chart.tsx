"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  LineStyle,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPaneApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type CandlestickData,
  type LineData,
  type HistogramData,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import type { Bar } from "@/lib/api";
import { useLens } from "@/lib/lens";
import { useBacktest, useTrades } from "@/lib/queries";
import { useStream, type WsMessage } from "@/lib/ws";
import { t } from "@/lib/i18n";
import {
  ChartCrosshairTooltip,
  type CrosshairData,
  type CrosshairIndicators,
} from "./ChartCrosshairTooltip";
import { HiLoBadge } from "./HiLoBadge";

export type IndicatorState = {
  ma: { enabled: boolean; period: number; kind: "sma" | "ema" };
  macd: { enabled: boolean };
  rsi: { enabled: boolean; period: number };
  kd: { enabled: boolean };
  dmi: { enabled: boolean };
};

type Props = {
  res: string;
  bars: Bar[];
  indicators: Record<string, Array<{ time: number } & Record<string, number | null>>>;
  state: IndicatorState;
  onSignal?: (m: WsMessage) => void;
  strategy?: string | null;
};

// TW market convention: red = up 漲, green = down 跌
const UP = "#c0392b";
const DOWN = "#3a7d4f";
const INK = "#1f1d1a";
const ACCENT = "#a8773d";
const TZ = "Asia/Taipei";

// V4 Phase 3 Slice B — per-strategy plot palette. lightweight-charts paints
// markers on a `<canvas>` and does NOT resolve CSS custom properties at draw
// time, so the SeriesMarker `color` field needs an explicit hex literal.
// CSS tokens still drive any DOM-visible chrome that wants to match.
const STRATEGY_COLORS: Record<string, { token: string; hex: string }> = {
  trade_strat_v1: { token: "var(--strategy-1)", hex: "#4a7ba6" },
  trade_strat_v2: { token: "var(--strategy-2)", hex: "#b87333" },
};

function strategyHex(name: string | null | undefined): string {
  if (!name) return "#8a8175";
  return STRATEGY_COLORS[name]?.hex ?? "#8a8175";
}

type IndKey = "macd" | "rsi" | "kd" | "dmi";

type PaneRefs = {
  pane: IPaneApi<Time> | null;
  series: ISeriesApi<any>[];
};

// `strategy` is still part of the Props contract (callers like
// `trading/page.tsx` keep passing it), but the chart itself no longer
// reads it directly — live trades render for ALL strategies (color-coded)
// and the backtest layer is driven by the URL lens. Destructure-ignored.
export function Chart({ res, bars, indicators, state, onSignal }: Props) {
  const qc = useQueryClient();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const maRef = useRef<ISeriesApi<"Line"> | null>(null);

  // Sub-pane refs — each entry exists only while its indicator is enabled.
  const macdRef = useRef<{
    pane: IPaneApi<Time>;
    line: ISeriesApi<"Line">;
    sig: ISeriesApi<"Line">;
    hist: ISeriesApi<"Histogram">;
  } | null>(null);
  const rsiRef = useRef<{ pane: IPaneApi<Time>; line: ISeriesApi<"Line"> } | null>(null);
  const kdRef = useRef<{ pane: IPaneApi<Time>; k: ISeriesApi<"Line">; d: ISeriesApi<"Line"> } | null>(null);
  const dmiRef = useRef<{
    pane: IPaneApi<Time>;
    plus: ISeriesApi<"Line">;
    minus: ISeriesApi<"Line">;
    adx: ISeriesApi<"Line">;
  } | null>(null);

  const [tooltip, setTooltip] = useState<CrosshairData | null>(null);

  // Strategy plot overlays — markers (entry/exit) accumulate in markersListRef
  // and are flushed via markersRef.setMarkers(). Active SL/TP/entry lines are
  // recreated per LONG/SHORT signal and torn down on the matching EXIT.
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const markersListRef = useRef<SeriesMarker<Time>[]>([]);
  const entryLineRef = useRef<IPriceLine | null>(null);
  const tpLineRef = useRef<IPriceLine | null>(null);
  const slLineRef = useRef<IPriceLine | null>(null);
  // Namespaced dedup keys: `live:${signalId}` for WS-driven entries,
  // `backtest:${strategy}:${tradeId}` for backtest-derived markers. Keeping
  // them in a single set avoids accidental collisions.
  const seenSignalIdsRef = useRef<Set<string>>(new Set());
  // Per-strategy LineSeries holding entry→exit segments. Each strategy gets
  // its own series so the canvas paints in the strategy color. Segments are
  // joined by `whitespace` entries (a LineData point with `value: undefined`)
  // — the chart breaks the line at those gaps so the segments don't visually
  // connect across unrelated trades.
  const connectorRefs = useRef<Map<string, ISeriesApi<"Line">>>(new Map());

  // V4 Phase 3 Slice B — backtest plot overlay. When the lens is active and a
  // /backtest/run result is in cache, render its trades as a parallel
  // (hollow / half-opacity) layer alongside the live one.
  const btMarkersListRef = useRef<SeriesMarker<Time>[]>([]);
  const btConnectorRefs = useRef<Map<string, ISeriesApi<"Line">>>(new Map());

  // Lookup tables — time(epoch s) → indicator values. Updated whenever the
  // upstream `indicators` payload changes; the crosshair handler reads from
  // these refs to avoid re-fetching on hover.
  const lookups = useRef({
    bars: new Map<number, { open: number; high: number; low: number; close: number }>(),
    ma: new Map<number, number>(),
    macd: new Map<number, { macd: number; signal: number; hist: number }>(),
    rsi: new Map<number, number>(),
    kd: new Map<number, { k: number; d: number }>(),
    dmi: new Map<number, { plus: number; minus: number; adx: number }>(),
  });

  // Persistent reactive state we read inside the crosshair callback —
  // refs avoid re-subscribing the handler on every state change.
  const stateRef = useRef(state);
  stateRef.current = state;

  // ─── Init chart ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;
    const reduceMotion =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { color: "#fbf7ee" }, textColor: INK },
      grid: { vertLines: { color: "#ece5d6" }, horzLines: { color: "#ece5d6" } },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: "#e3dccf",
        // lightweight-charts has no native tz option — we render every tick
        // mark via Intl.DateTimeFormat pinned to Asia/Taipei so the x-axis
        // matches local trading hours (08:45–13:45 CST).
        tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) => {
          const epochSec = typeof time === "number" ? time : 0;
          const d = new Date(epochSec * 1000);
          const opts: Intl.DateTimeFormatOptions = { timeZone: TZ };
          switch (tickMarkType) {
            case TickMarkType.Year:
              opts.year = "numeric";
              break;
            case TickMarkType.Month:
              opts.year = "numeric";
              opts.month = "short";
              break;
            case TickMarkType.DayOfMonth:
              opts.month = "numeric";
              opts.day = "numeric";
              break;
            case TickMarkType.Time:
            case TickMarkType.TimeWithSeconds:
            default:
              opts.hour = "2-digit";
              opts.minute = "2-digit";
              opts.hour12 = false;
              if (tickMarkType === TickMarkType.TimeWithSeconds) opts.second = "2-digit";
              break;
          }
          return new Intl.DateTimeFormat("zh-Hant-TW", opts).format(d);
        },
      },
      localization: {
        locale: "zh-Hant-TW",
        timeFormatter: (time: Time) => {
          const epochSec = typeof time === "number" ? time : 0;
          return new Date(epochSec * 1000).toLocaleString("zh-Hant-TW", {
            timeZone: TZ,
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          });
        },
      },
      rightPriceScale: { borderColor: "#e3dccf" },
      crosshair: { vertLine: { color: "#8a8175" }, horzLine: { color: "#8a8175" } },
    });
    chartRef.current = chart;
    candleRef.current = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
    });
    if (reduceMotion) {
      candleRef.current.applyOptions({ priceLineVisible: false });
    }
    markersRef.current = createSeriesMarkers(candleRef.current, []);

    const onMove = (param: MouseEventParams<Time>) => {
      // Cursor price is independent of whether a candle exists at param.time —
      // we want it visible everywhere inside the chart pane, including over
      // candles and inside the gaps between them.
      let cursorPrice: number | null = null;
      if (param.point && param.point.y != null && candleRef.current) {
        const v = candleRef.current.coordinateToPrice(param.point.y);
        if (v != null && Number.isFinite(v as number)) {
          cursorPrice = Number(v);
        }
      }
      // No cursor inside the chart at all — clear tooltip.
      if (!param.point) {
        setTooltip(null);
        return;
      }
      const time = param.time != null ? Number(param.time) : null;
      const bar = time != null ? lookups.current.bars.get(time) : undefined;
      const ind: CrosshairIndicators = {};
      if (time != null) {
        const s = stateRef.current;
        if (s.ma.enabled) {
          const v = lookups.current.ma.get(time);
          if (v != null) ind.ma = { period: s.ma.period, value: v };
        }
        if (s.macd.enabled) {
          const v = lookups.current.macd.get(time);
          if (v) ind.macd = v;
        }
        if (s.rsi.enabled) {
          const v = lookups.current.rsi.get(time);
          if (v != null) ind.rsi = { value: v };
        }
        if (s.kd.enabled) {
          const v = lookups.current.kd.get(time);
          if (v) ind.kd = v;
        }
        if (s.dmi.enabled) {
          const v = lookups.current.dmi.get(time);
          if (v) ind.dmi = v;
        }
      }
      setTooltip({
        ohlc: bar && time != null ? { time, ...bar } : null,
        indicators: ind,
        cursorPrice,
      });
    };
    chart.subscribeCrosshairMove(onMove);

    // V4 Phase 5 — global "scroll chart to time" hook. AlertLog rows dispatch
    // a `chart-scroll-to` CustomEvent with `{ time: epochSeconds }`; we
    // recenter the visible range on that timestamp using a ±3-hour window.
    // lightweight-charts has no direct `scrollTo(time)`; setVisibleRange is
    // the v5-supported equivalent.
    const onScrollTo = (e: Event) => {
      const ev = e as CustomEvent<{ time: number }>;
      const time = ev.detail?.time;
      if (typeof time !== "number" || !Number.isFinite(time) || !chartRef.current) return;
      const halfWindow = 3600 * 3;
      try {
        chartRef.current.timeScale().setVisibleRange({
          from: (time - halfWindow) as Time,
          to: (time + halfWindow) as Time,
        });
      } catch {
        // setVisibleRange throws if the requested range falls entirely
        // outside the data — silently ignore; the user can re-scroll.
      }
    };
    if (typeof window !== "undefined") {
      window.addEventListener("chart-scroll-to", onScrollTo);
    }

    return () => {
      chart.unsubscribeCrosshairMove(onMove);
      if (typeof window !== "undefined") {
        window.removeEventListener("chart-scroll-to", onScrollTo);
      }
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      maRef.current = null;
      macdRef.current = null;
      rsiRef.current = null;
      kdRef.current = null;
      dmiRef.current = null;
      markersRef.current = null;
      markersListRef.current = [];
      entryLineRef.current = null;
      tpLineRef.current = null;
      slLineRef.current = null;
      seenSignalIdsRef.current = new Set();
      // The chart was just removed; the underlying series are gone with it.
      // Clear our handle map so a fresh mount doesn't reuse dead refs.
      connectorRefs.current.clear();
      btMarkersListRef.current = [];
      btConnectorRefs.current.clear();
    };
  }, []);

  // ─── Push bars ─────────────────────────────────────────────────────────
  // After Track B, `bars` is closed-history only — the WS owns the live
  // in-progress bar. Refetches must NOT clobber `lastBarRef`; instead, we
  // setData the historical array, then re-overlay the live bar (if any) via
  // `update(...)` so the in-progress candle keeps growing smoothly.
  //
  // EXCEPT on resolution change: a 1m bucket time can be > the 5m bucket time
  // for the same instant (e.g. 13:42 > 13:40), so a stale `lastBarRef` from
  // the previous resolution would pass the `live.time > lastHistTime` check
  // and paint a 1m candle into the 5m series. Detect res transitions via a
  // separate ref and clear `lastBarRef` BEFORE the overlay logic runs.
  const prevResRef = useRef<string | null>(null);
  useEffect(() => {
    if (!candleRef.current) return;
    if (prevResRef.current !== null && prevResRef.current !== res) {
      lastBarRef.current = null;
    }
    prevResRef.current = res;
    const data: CandlestickData<Time>[] = bars.map((b) => ({
      time: b.time as Time,
      open: b.open, high: b.high, low: b.low, close: b.close,
    }));
    candleRef.current.setData(data);

    const map = new Map<number, { open: number; high: number; low: number; close: number }>();
    for (const b of bars) map.set(b.time, { open: b.open, high: b.high, low: b.low, close: b.close });

    // Re-overlay the live bar if it sits strictly after the last historical
    // bar (i.e. it represents the still-open bucket). On resolution change,
    // the time-axis is different and the previous live bar is meaningless —
    // detect that by checking whether `lastBarRef.current.time` falls inside
    // the new bars range; if so but it doesn't match the latest bar, drop it.
    const live = lastBarRef.current;
    const lastHistTime = bars.length ? bars[bars.length - 1].time : null;
    if (live) {
      if (lastHistTime != null && live.time > lastHistTime) {
        // Live bar is newer than all closed bars — overlay it.
        candleRef.current.update({
          time: live.time as Time,
          open: live.open,
          high: live.high,
          low: live.low,
          close: live.close,
        });
        map.set(live.time, {
          open: live.open,
          high: live.high,
          low: live.low,
          close: live.close,
        });
      } else if (lastHistTime == null) {
        // Empty history but we have a live bar (rare: fresh WS-only start).
        // Re-apply it so setData([]) above doesn't blank the chart.
        candleRef.current.update({
          time: live.time as Time,
          open: live.open,
          high: live.high,
          low: live.low,
          close: live.close,
        });
        map.set(live.time, {
          open: live.open,
          high: live.high,
          low: live.low,
          close: live.close,
        });
      }
      // Otherwise: live.time <= lastHistTime — likely a resolution change or
      // the backend caught up and now includes what was the live bar. Let the
      // re-seed effect below pick a fresh `lastBarRef` from the new bars.
    }
    lookups.current.bars = map;
  }, [bars, res]);

  const wantMA = state.ma.enabled && !!indicators.ma;
  const wantMACD = state.macd.enabled && !!indicators.macd;
  const wantRSI = state.rsi.enabled && !!indicators.rsi;
  const wantKD = state.kd.enabled && !!indicators.kd;
  const wantDMI = state.dmi.enabled && !!indicators.dmi;

  // ─── MA overlay (price pane = pane 0) ─────────────────────────────────
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (!wantMA) {
      if (maRef.current) { chart.removeSeries(maRef.current); maRef.current = null; }
      lookups.current.ma = new Map();
      return;
    }
    if (!maRef.current) {
      maRef.current = chart.addSeries(LineSeries, { color: ACCENT, lineWidth: 1 }, 0);
    }
    const rows = indicators.ma ?? [];
    const data: LineData<Time>[] = rows
      .filter((p) => p.ma != null)
      .map((p) => ({ time: p.time as Time, value: p.ma as number }));
    maRef.current.setData(data);

    const m = new Map<number, number>();
    for (const p of rows) {
      if (p.ma != null) m.set(p.time, p.ma as number);
    }
    lookups.current.ma = m;
  }, [indicators.ma, wantMA]);

  // ─── MACD pane ────────────────────────────────────────────────────────
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const teardown = () => {
      if (macdRef.current) {
        chart.removeSeries(macdRef.current.line);
        chart.removeSeries(macdRef.current.sig);
        chart.removeSeries(macdRef.current.hist);
        try { chart.removePane(macdRef.current.pane.paneIndex()); } catch {}
        macdRef.current = null;
      }
      lookups.current.macd = new Map();
    };
    if (!wantMACD) { teardown(); return; }
    if (!macdRef.current) {
      const pane = chart.addPane();
      pane.setHeight(120);
      const idx = pane.paneIndex();
      const opts = { priceFormat: { type: "price", precision: 2, minMove: 0.01 } } as const;
      const hist = chart.addSeries(HistogramSeries, { ...opts, color: "#8a8175" }, idx);
      const line = chart.addSeries(LineSeries, { ...opts, color: INK, lineWidth: 1 }, idx);
      const sig = chart.addSeries(LineSeries, { ...opts, color: ACCENT, lineWidth: 1 }, idx);
      macdRef.current = { pane, line, sig, hist };
    }
    const rows = indicators.macd ?? [];
    const histData: HistogramData<Time>[] = rows
      .filter((p) => p.hist != null)
      .map((p) => ({
        time: p.time as Time,
        value: p.hist as number,
        color: (p.hist as number) >= 0 ? `${UP}55` : `${DOWN}55`,
      }));
    const lineData: LineData<Time>[] = rows.filter((p) => p.macd != null).map((p) => ({ time: p.time as Time, value: p.macd as number }));
    const sigData: LineData<Time>[] = rows.filter((p) => p.signal != null).map((p) => ({ time: p.time as Time, value: p.signal as number }));
    macdRef.current.hist.setData(histData);
    macdRef.current.line.setData(lineData);
    macdRef.current.sig.setData(sigData);

    const m = new Map<number, { macd: number; signal: number; hist: number }>();
    for (const p of rows) {
      if (p.macd != null && p.signal != null && p.hist != null) {
        m.set(p.time, { macd: p.macd as number, signal: p.signal as number, hist: p.hist as number });
      }
    }
    lookups.current.macd = m;
  }, [indicators.macd, wantMACD]);

  // ─── RSI pane ─────────────────────────────────────────────────────────
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const teardown = () => {
      if (rsiRef.current) {
        chart.removeSeries(rsiRef.current.line);
        try { chart.removePane(rsiRef.current.pane.paneIndex()); } catch {}
        rsiRef.current = null;
      }
      lookups.current.rsi = new Map();
    };
    if (!wantRSI) { teardown(); return; }
    if (!rsiRef.current) {
      const pane = chart.addPane();
      pane.setHeight(100);
      const idx = pane.paneIndex();
      const line = chart.addSeries(LineSeries, { color: "#5d3f6e", lineWidth: 1 }, idx);
      rsiRef.current = { pane, line };
    }
    const rows = (indicators.rsi ?? []).filter((p) => p.rsi != null);
    rsiRef.current.line.setData(rows.map((p) => ({ time: p.time as Time, value: p.rsi as number })));

    const m = new Map<number, number>();
    for (const p of rows) m.set(p.time, p.rsi as number);
    lookups.current.rsi = m;
  }, [indicators.rsi, wantRSI]);

  // ─── KD pane ──────────────────────────────────────────────────────────
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const teardown = () => {
      if (kdRef.current) {
        chart.removeSeries(kdRef.current.k);
        chart.removeSeries(kdRef.current.d);
        try { chart.removePane(kdRef.current.pane.paneIndex()); } catch {}
        kdRef.current = null;
      }
      lookups.current.kd = new Map();
    };
    if (!wantKD) { teardown(); return; }
    if (!kdRef.current) {
      const pane = chart.addPane();
      pane.setHeight(100);
      const idx = pane.paneIndex();
      const k = chart.addSeries(LineSeries, { color: "#2a6f5a", lineWidth: 1 }, idx);
      const d = chart.addSeries(LineSeries, { color: ACCENT, lineWidth: 1 }, idx);
      kdRef.current = { pane, k, d };
    }
    const rows = indicators.kd ?? [];
    kdRef.current.k.setData(rows.filter((p) => p.k != null).map((p) => ({ time: p.time as Time, value: p.k as number })));
    kdRef.current.d.setData(rows.filter((p) => p.d != null).map((p) => ({ time: p.time as Time, value: p.d as number })));

    const m = new Map<number, { k: number; d: number }>();
    for (const p of rows) {
      if (p.k != null && p.d != null) m.set(p.time, { k: p.k as number, d: p.d as number });
    }
    lookups.current.kd = m;
  }, [indicators.kd, wantKD]);

  // ─── DMI pane ─────────────────────────────────────────────────────────
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const teardown = () => {
      if (dmiRef.current) {
        chart.removeSeries(dmiRef.current.plus);
        chart.removeSeries(dmiRef.current.minus);
        chart.removeSeries(dmiRef.current.adx);
        try { chart.removePane(dmiRef.current.pane.paneIndex()); } catch {}
        dmiRef.current = null;
      }
      lookups.current.dmi = new Map();
    };
    if (!wantDMI) { teardown(); return; }
    if (!dmiRef.current) {
      const pane = chart.addPane();
      pane.setHeight(100);
      const idx = pane.paneIndex();
      // +DI = 漲 = 紅; −DI = 跌 = 綠 (TW); ADX = sumi-gold
      const plus = chart.addSeries(LineSeries, { color: UP, lineWidth: 1 }, idx);
      const minus = chart.addSeries(LineSeries, { color: DOWN, lineWidth: 1 }, idx);
      const adx = chart.addSeries(LineSeries, { color: ACCENT, lineWidth: 1 }, idx);
      dmiRef.current = { pane, plus, minus, adx };
    }
    const rows = indicators.dmi ?? [];
    dmiRef.current.plus.setData(rows.filter((p) => p.plus_di != null).map((p) => ({ time: p.time as Time, value: p.plus_di as number })));
    dmiRef.current.minus.setData(rows.filter((p) => p.minus_di != null).map((p) => ({ time: p.time as Time, value: p.minus_di as number })));
    dmiRef.current.adx.setData(rows.filter((p) => p.adx != null).map((p) => ({ time: p.time as Time, value: p.adx as number })));

    const m = new Map<number, { plus: number; minus: number; adx: number }>();
    for (const p of rows) {
      if (p.plus_di != null && p.minus_di != null && p.adx != null) {
        m.set(p.time, { plus: p.plus_di as number, minus: p.minus_di as number, adx: p.adx as number });
      }
    }
    lookups.current.dmi = m;
  }, [indicators.dmi, wantDMI]);

  // ─── Live entry → exit connectors (per-strategy) ──────────────────────
  // Pulls ALL closed trades (no strategy filter — let the rail / lens drive
  // scope) and renders one solid LineSeries per strategy in the strategy
  // color. Segments inside one series are separated by a whitespace data
  // point (no `value`) so lightweight-charts breaks the line between
  // unrelated trades. Out-of-window segments simply hide; LWC requires
  // monotonic time, so trades are sorted per strategy.
  const tradesQ = useTrades({
    result: "all",
    limit: 200,
  });
  const trades = tradesQ.data;
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    type ClosedTrade = NonNullable<typeof trades>[number];
    const byStrat = new Map<string, ClosedTrade[]>();
    if (trades) {
      for (const tr of trades) {
        if (!tr.exit_ts || tr.exit_price == null) continue;
        const arr = byStrat.get(tr.strategy) ?? [];
        arr.push(tr);
        byStrat.set(tr.strategy, arr);
      }
    }
    // Tear down series for strategies that no longer have trades in scope.
    for (const [name, series] of connectorRefs.current) {
      if (!byStrat.has(name)) {
        try { chart.removeSeries(series); } catch {}
        connectorRefs.current.delete(name);
      }
    }
    // For each strategy with closed trades, ensure a series and update data.
    for (const [name, list] of byStrat) {
      let series = connectorRefs.current.get(name);
      if (!series) {
        series = chart.addSeries(
          LineSeries,
          {
            color: strategyHex(name),
            lineWidth: 1,
            lineStyle: LineStyle.Solid,
            priceLineVisible: false,
            lastValueVisible: false,
          },
          0,
        );
        connectorRefs.current.set(name, series);
      } else {
        series.applyOptions({ color: strategyHex(name) });
      }
      type Pt = { time: Time; value?: number };
      const sorted = [...list].sort(
        (a, b) =>
          new Date(a.entry_ts).getTime() - new Date(b.entry_ts).getTime(),
      );
      const data: Pt[] = [];
      for (const tr of sorted) {
        const tEntry = Math.floor(new Date(tr.entry_ts).getTime() / 1000);
        const tExit = Math.floor(new Date(tr.exit_ts as string).getTime() / 1000);
        if (tExit <= tEntry) continue;
        data.push({ time: tEntry as Time, value: tr.entry_price });
        data.push({ time: tExit as Time, value: tr.exit_price as number });
        // whitespace separator: time strictly after exit but before any next
        // entry; +1 second is enough since LWC requires strictly monotonic time.
        data.push({ time: (tExit + 1) as Time });
      }
      series.setData(data);
    }
  }, [trades]);

  // ─── Permanent live markers from the trades table ─────────────────────
  // Each closed trade contributes an entry arrow + exit circle; each open
  // trade contributes an entry arrow only. Tinted by strategy. Re-runs on
  // every `trades` change, so newly persisted trades appear without a
  // page reload. WS signal arrivals invalidate the trades query (see WS
  // handler below) so the marker layer follows the live position tracker.
  useEffect(() => {
    const list: SeriesMarker<Time>[] = [];
    if (trades) {
      const sorted = [...trades].sort(
        (a, b) =>
          new Date(a.entry_ts).getTime() - new Date(b.entry_ts).getTime(),
      );
      for (const tr of sorted) {
        const stratColor = strategyHex(tr.strategy);
        const tEntry = Math.floor(new Date(tr.entry_ts).getTime() / 1000) as Time;
        const isLong = tr.side === "LONG";
        list.push({
          time: tEntry,
          position: isLong ? "belowBar" : "aboveBar",
          color: stratColor,
          shape: isLong ? "arrowUp" : "arrowDown",
          text: tr.side,
        });
        if (tr.exit_ts && tr.exit_price != null && tr.pnl_points != null) {
          const tExit = Math.floor(new Date(tr.exit_ts).getTime() / 1000) as Time;
          list.push({
            time: tExit,
            position: tr.pnl_points >= 0 ? "aboveBar" : "belowBar",
            color: stratColor,
            shape: "circle",
            text: `${tr.pnl_points >= 0 ? "+" : ""}${tr.pnl_points.toFixed(0)}`,
          });
        }
      }
    }
    markersListRef.current = list;
    markersRef.current?.setMarkers([
      ...markersListRef.current,
      ...btMarkersListRef.current,
    ]);
  }, [trades]);

  // ─── Backtest plot overlay (lens-driven) ──────────────────────────────
  // When the lens is active (s + start + end), pull the cached
  // /backtest/run result and render trades as a half-opacity dotted layer
  // alongside the live one. Markers are merged via btMarkersListRef so a
  // new live signal arriving via WS doesn't blow them away.
  const lens = useLens();
  const btReq =
    lens.isActive && lens.strategy && lens.start && lens.end
      ? { strategy: lens.strategy, start: lens.start, end: lens.end }
      : null;
  const btQ = useBacktest(btReq);
  const btTrades = btQ.data?.trades;

  useEffect(() => {
    const chart = chartRef.current;
    const stratName = lens.strategy;
    if (!chart || !stratName || !btTrades || btTrades.length === 0) {
      // Tear down all backtest connectors when the lens goes inactive or the
      // result is empty.
      for (const [, s] of btConnectorRefs.current) {
        try {
          chart?.removeSeries(s);
        } catch {}
      }
      btConnectorRefs.current.clear();
      return;
    }
    let series = btConnectorRefs.current.get(stratName);
    const halfOpacityColor = strategyHex(stratName) + "99"; // ~60%
    if (!series) {
      series = chart.addSeries(
        LineSeries,
        {
          color: halfOpacityColor,
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          priceLineVisible: false,
          lastValueVisible: false,
        },
        0,
      );
      btConnectorRefs.current.set(stratName, series);
    } else {
      series.applyOptions({ color: halfOpacityColor });
    }
    // Drop any stale series for other strategies (e.g. switched lens target).
    for (const [name, s] of btConnectorRefs.current) {
      if (name === stratName) continue;
      try { chart.removeSeries(s); } catch {}
      btConnectorRefs.current.delete(name);
    }
    type Pt = { time: Time; value?: number };
    const sorted = [...btTrades].sort(
      (a, b) => new Date(a.entry_ts).getTime() - new Date(b.entry_ts).getTime(),
    );
    const data: Pt[] = [];
    for (const tr of sorted) {
      const tEntry = Math.floor(new Date(tr.entry_ts).getTime() / 1000);
      const tExit = Math.floor(new Date(tr.exit_ts).getTime() / 1000);
      if (tExit <= tEntry) continue;
      data.push({ time: tEntry as Time, value: tr.entry_price });
      data.push({ time: tExit as Time, value: tr.exit_price });
      data.push({ time: (tExit + 1) as Time });
    }
    series.setData(data);
  }, [btTrades, lens.strategy]);

  useEffect(() => {
    // Build the backtest marker layer from the cached trades, then re-flush
    // the merged (live + backtest) marker list so neither layer clobbers
    // the other on re-render.
    const stratName = lens.strategy;
    if (!btTrades || !stratName) {
      btMarkersListRef.current = [];
    } else {
      const baseColor = strategyHex(stratName) + "AA"; // ~67%
      const list: SeriesMarker<Time>[] = [];
      for (const tr of btTrades) {
        const tEntry = Math.floor(new Date(tr.entry_ts).getTime() / 1000) as Time;
        const tExit = Math.floor(new Date(tr.exit_ts).getTime() / 1000) as Time;
        const isLong = tr.side === "LONG";
        list.push({
          time: tEntry,
          position: isLong ? "belowBar" : "aboveBar",
          color: baseColor,
          shape: isLong ? "arrowUp" : "arrowDown",
          text: tr.side,
        });
        list.push({
          time: tExit,
          position: tr.pnl_points >= 0 ? "aboveBar" : "belowBar",
          color: baseColor,
          shape: "circle",
          text: `BT ${tr.pnl_points >= 0 ? "+" : ""}${tr.pnl_points.toFixed(0)}`,
        });
      }
      btMarkersListRef.current = list;
    }
    const merged = [...markersListRef.current, ...btMarkersListRef.current];
    markersRef.current?.setMarkers(merged);
  }, [btTrades, lens.strategy]);

  // ─── Live updates from WS ─────────────────────────────────────────────
  const lastBarRef = useRef<{ time: number; open: number; high: number; low: number; close: number } | null>(null);
  // Re-seed only when:
  //   (a) we have nothing yet, or
  //   (b) the new history's last bar is strictly newer than the live state
  //       (e.g. resolution changed and we got a fresh time-axis), or
  //   (c) the new history is empty (nothing to seed from — leave WS to fill).
  // Same-time refetches (lastHist.time === live.time) MUST keep the live
  // ref intact so the WS-accumulated O/H/L/C is not stomped back to the
  // stale cagg snapshot. Older live-than-history (resolution change) is
  // also a hard reset.
  useEffect(() => {
    if (bars.length === 0) {
      if (lastBarRef.current === null) return;
      // New empty history while we have a live bar from a previous resolution
      // — clear it so the WS handler treats the next update as a fresh bar.
      lastBarRef.current = null;
      return;
    }
    const lastHist = bars[bars.length - 1];
    const live = lastBarRef.current;
    if (live === null || lastHist.time > live.time || lastHist.time < live.time) {
      // (a) no live state yet, or (b) history advanced past live (rare —
      // WS missed a bar boundary), or (c) live points to a time outside the
      // new bars range (resolution change). In all three cases adopt the
      // new history's last bar as the seed.
      lastBarRef.current = { ...lastHist };
    }
    // lastHist.time === live.time — refetch arrived for the same bucket the
    // WS is accumulating. Keep `lastBarRef.current` (WS data is authoritative).
  }, [bars]);

  // ─── Strategy plot overlay (markers + SL/TP/entry price lines) ────────
  // Idempotent on signal id so a refetch / re-broadcast cannot double-mark.
  // LONG / SHORT — paint entry marker + entry/TP/SL price lines.
  // EXIT          — paint exit marker (color by exit_reason) + tear down lines.
  function drawSignalOverlay(m: Extract<WsMessage, { type: "signal" }>) {
    const series = candleRef.current;
    if (!series) return;
    if (m.id != null) {
      const key = `live:${m.id}`;
      if (seenSignalIdsRef.current.has(key)) return;
      seenSignalIdsRef.current.add(key);
    }
    const side = m.side;
    const payload = (m.payload ?? {}) as Record<string, unknown>;

    if (side === "LONG" || side === "SHORT") {
      const isLong = side === "LONG";

      // Tear down any leftover lines from a stale prior position before
      // drawing fresh ones (defensive — strategy guards against pyramiding
      // but ws replay or reload could leave lines around).
      removePositionLines();
      const tpPts = Number(payload.tp_points ?? 0);
      const slPts = Number(payload.sl_points ?? 0);
      const entry = m.price;
      const tp = isLong ? entry + tpPts : entry - tpPts;
      const sl = isLong ? entry - slPts : entry + slPts;
      entryLineRef.current = series.createPriceLine({
        price: entry,
        color: "#8a8175",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `entry ${side}`,
      });
      tpLineRef.current = series.createPriceLine({
        price: tp,
        color: UP,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: true,
        title: `TP +${tpPts}`,
      });
      slLineRef.current = series.createPriceLine({
        price: sl,
        color: DOWN,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: true,
        title: `SL -${slPts}`,
      });
      return;
    }

    if (side === "EXIT") {
      // Marker for the exit will be added by the trades-derived effect once
      // the position tracker writes the row. We just clean up the live
      // position lines here.
      removePositionLines();
    }
  }

  function removePositionLines() {
    const series = candleRef.current;
    if (!series) return;
    if (entryLineRef.current) {
      series.removePriceLine(entryLineRef.current);
      entryLineRef.current = null;
    }
    if (tpLineRef.current) {
      series.removePriceLine(tpLineRef.current);
      tpLineRef.current = null;
    }
    if (slLineRef.current) {
      series.removePriceLine(slLineRef.current);
      slLineRef.current = null;
    }
  }

  const connected = useStream(res, (m) => {
    if (m.type === "signal") {
      onSignal?.(m);
      drawSignalOverlay(m);
      // Refresh the trades query so the new entry / exit row (written by the
      // position tracker on the same signal) materializes as a permanent
      // marker without waiting for the next staleTime cycle.
      qc.invalidateQueries({ queryKey: ["trades"] });
      return;
    }
    if (!candleRef.current) return;
    if (m.type !== "bar_update") return;
    if (!candleRef.current) return;
    if (m.type !== "bar_update") return;
    const ts = Math.floor(new Date(m.bucket).getTime() / 1000);
    const price = m.price;
    const last = lastBarRef.current;
    if (!last || ts !== last.time) {
      const newBar = { time: ts, open: price, high: price, low: price, close: price };
      lastBarRef.current = newBar;
      candleRef.current.update({ time: ts as Time, open: price, high: price, low: price, close: price });
      lookups.current.bars.set(ts, { open: price, high: price, low: price, close: price });
    } else {
      last.high = Math.max(last.high, price);
      last.low = Math.min(last.low, price);
      last.close = price;
      candleRef.current.update({ time: ts as Time, open: last.open, high: last.high, low: last.low, close: last.close });
      lookups.current.bars.set(ts, { open: last.open, high: last.high, low: last.low, close: last.close });
    }
  });

  const status = useMemo(() => connected ? t("status_live") : t("status_reconnecting"), [connected]);

  return (
    <div style={{ position: "relative", height: "100%" }}>
      <div ref={containerRef} className="chart" style={{ height: "100%" }} />
      <ChartCrosshairTooltip data={tooltip} />
      <div style={{ position: "absolute", top: 6, right: 10, fontSize: 11, color: connected ? "var(--down)" : "var(--warn)" }}>
        {status}
      </div>
      {tooltip?.ohlc && (
        <HiLoBadge hi={tooltip.ohlc.high} lo={tooltip.ohlc.low} />
      )}
    </div>
  );
}
