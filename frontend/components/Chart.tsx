"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  LineStyle,
  TickMarkType,
  createChart,
  type IChartApi,
  type IPaneApi,
  type IPriceLine,
  type ISeriesApi,
  type CandlestickData,
  type LineData,
  type HistogramData,
  type MouseEventParams,
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
import { TradeMarkerTooltip } from "./TradeMarkerTooltip";

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

export type TradeEvent = {
  tradeId: number;
  time: number;
  price: number;
  kind: "OPEN" | "CLOSE";
  side: string;
  strategy: string;
  reason: string;
  pnl?: number;
  source: "LIVE" | "BACKTEST";
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
  trade_strat_v1: { token: "var(--strategy-1)", hex: "#1e88e5" },
  trade_strat_v2: { token: "var(--strategy-2)", hex: "#fb8c00" },
};

// Pixel offset applied to every marker so the dot sits to the right of the
// candle wick instead of overlapping the candle body. Keeps the chart
// readable even with several markers stacked on adjacent bars.
const MARKER_X_OFFSET = 14;

function strategyHex(name: string | null | undefined): string {
  if (!name) return "#8a8175";
  return STRATEGY_COLORS[name]?.hex ?? "#8a8175";
}

// Paint one trade marker on the overlay canvas. Both OPEN and CLOSE render
// as a labelled disc — the Traditional Chinese glyph (進 / 出) inside the
// dot identifies the event without requiring hover.
//
//   OPEN  → filled disc, fill = strategy color, white halo, "進" glyph
//   CLOSE → filled disc, fill = win-red / loss-green (TW palette), 2 px
//           ring in strategy color, white halo, "出" glyph
//
// Hover state: scale up + soft strategy-tinted halo + outline ring. The dot
// sits MARKER_X_OFFSET pixels to the right of the candle so it doesn't
// overlap candle bodies.
function paintMarker(
  ctx: CanvasRenderingContext2D,
  ev: TradeEvent,
  x: number,
  y: number,
  hovered: boolean,
): void {
  const stratColor = strategyHex(ev.strategy);
  const isClose = ev.kind === "CLOSE";
  const isWin = isClose && (ev.pnl ?? 0) >= 0;
  const fill = isClose ? (isWin ? UP : DOWN) : stratColor;
  const glyph = isClose ? "出" : "進";
  const r = hovered ? 13 : 11;

  ctx.save();

  // Hover halo — soft fill + outline ring around the marker.
  if (hovered) {
    ctx.beginPath();
    ctx.arc(x, y, r + 6, 0, Math.PI * 2);
    ctx.fillStyle = stratColor + "26";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x, y, r + 4, 0, Math.PI * 2);
    ctx.strokeStyle = stratColor;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  // White halo behind the disc so the dot reads against any candle color.
  ctx.beginPath();
  ctx.arc(x, y, r + 1.5, 0, Math.PI * 2);
  ctx.fillStyle = "#ffffff";
  ctx.fill();

  // Filled disc.
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = fill;
  ctx.fill();

  // Strategy ring on CLOSE so the win/loss fill doesn't lose the strategy
  // attribution. OPEN already uses the strategy color as its fill.
  if (isClose) {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.strokeStyle = stratColor;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // TC glyph in white — bold for legibility at small sizes.
  ctx.fillStyle = "#ffffff";
  ctx.font = `bold ${Math.round(r * 1.05)}px "Noto Sans TC", "PingFang TC", system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(glyph, x, y + 1);

  ctx.restore();
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
  // Custom canvas overlay for trade markers — replaces lightweight-charts'
  // built-in createSeriesMarkers so we can pixel hit-test on the dot itself
  // and own the visual treatment (shape grammar OPEN vs CLOSE, win/loss tint,
  // strategy ring, hover affordance). The canvas sits absolutely over the
  // chart container with pointer-events: none so chart drag/zoom still work.
  const overlayRef = useRef<HTMLCanvasElement | null>(null);
  const [hoveredEvent, setHoveredEvent] = useState<TradeEvent | null>(null);
  const [hoveredPos, setHoveredPos] = useState<{ x: number; y: number } | null>(null);
  // Live SL/TP/entry price lines for the open position.
  const entryLineRef = useRef<IPriceLine | null>(null);
  const tpLineRef = useRef<IPriceLine | null>(null);
  const slLineRef = useRef<IPriceLine | null>(null);
  // Live signal dedup so WS replay can't double-paint entry/TP/SL price lines.
  const seenSignalIdsRef = useRef<Set<string>>(new Set());

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
      layout: {
        background: { color: "#fbf7ee" },
        textColor: INK,
        // Make the pane separator visible + draggable. lightweight-charts v5
        // exposes color/hover styling but no separator thickness option.
        // Keep the row height at the library's measured 1px so autoSize and
        // pane layout calculations stay in sync.
        panes: {
          separatorColor: "#a8773d",
          separatorHoverColor: "#1f1d1a",
          enableResize: true,
        },
      },
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
      entryLineRef.current = null;
      tpLineRef.current = null;
      slLineRef.current = null;
      seenSignalIdsRef.current = new Set();
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

  // ─── Unified trade events (live + backtest) ───────────────────────────
  // One marker layer for all open/close events regardless of source. Markers
  // are small dots — no inline text labels (those collided with candles).
  // Hover detail comes from the crosshair tooltip via tradeEventsByTime.
  const tradesQ = useTrades({ result: "all", limit: 200 });
  const trades = tradesQ.data;

  const lens = useLens();
  const btReq =
    lens.isActive && lens.strategy && lens.start && lens.end
      ? { strategy: lens.strategy, start: lens.start, end: lens.end }
      : null;
  const btQ = useBacktest(btReq);
  const btTrades = btQ.data?.trades;

  const tradeEvents = useMemo<TradeEvent[]>(() => {
    const out: TradeEvent[] = [];
    if (trades) {
      for (const tr of trades) {
        const payload = (tr.payload ?? {}) as Record<string, unknown>;
        const entryReason =
          (payload.entry_reason as string | undefined) ?? "";
        const exitReason = (payload.exit_reason as string | undefined) ?? "";
        out.push({
          tradeId: tr.id,
          time: Math.floor(new Date(tr.entry_ts).getTime() / 1000),
          price: tr.entry_price,
          kind: "OPEN",
          side: tr.side,
          strategy: tr.strategy,
          reason: entryReason,
          source: "LIVE",
        });
        if (tr.exit_ts && tr.exit_price != null) {
          out.push({
            tradeId: tr.id,
            time: Math.floor(new Date(tr.exit_ts).getTime() / 1000),
            price: tr.exit_price,
            kind: "CLOSE",
            side: tr.side,
            strategy: tr.strategy,
            reason: exitReason,
            pnl: tr.pnl_points ?? undefined,
            source: "LIVE",
          });
        }
      }
    }
    if (btTrades && lens.strategy) {
      for (const tr of btTrades) {
        out.push({
          tradeId: tr.id,
          time: Math.floor(new Date(tr.entry_ts).getTime() / 1000),
          price: tr.entry_price,
          kind: "OPEN",
          side: tr.side,
          strategy: lens.strategy,
          reason: tr.entry_reason,
          source: "BACKTEST",
        });
        out.push({
          tradeId: tr.id,
          time: Math.floor(new Date(tr.exit_ts).getTime() / 1000),
          price: tr.exit_price,
          kind: "CLOSE",
          side: tr.side,
          strategy: lens.strategy,
          reason: tr.exit_reason,
          pnl: tr.pnl_points,
          source: "BACKTEST",
        });
      }
    }
    return out;
  }, [trades, btTrades, lens.strategy]);

  // ─── Custom marker overlay (canvas) ────────────────────────────────────
  // Lightweight-charts' built-in createSeriesMarkers paints into the same
  // canvas the chart owns and exposes no hover events, so a user pointing at
  // the dot itself never gets a tooltip — only when the cursor happens to
  // also intersect the underlying candle's logical time. We replace it with
  // our own canvas overlay where:
  //   * each event is painted via priceToCoordinate / timeToCoordinate
  //   * a mousemove listener on the chart container hit-tests against the
  //     event list (12px radius, nearest wins)
  //   * the hovered event drives both a highlight ring on the canvas and a
  //     dedicated <TradeMarkerTooltip> card (separate from the OHLC tooltip).
  //
  // OPEN markers: filled triangle pointing toward the entry bar (▲ LONG
  // below the bar / ▼ SHORT above), fill = strategy color, white halo.
  // CLOSE markers: filled circle, fill = win/loss (TW palette: red = win,
  // green = loss), 2px ring in strategy color, white halo.
  //
  // Triggers redraw on tradeEvents change, hoveredEvent change, visible
  // time-range change, container resize, and bar updates.
  const tradeEventsRef = useRef<TradeEvent[]>([]);
  useEffect(() => {
    tradeEventsRef.current = tradeEvents;
  }, [tradeEvents]);
  const hoveredEventRef = useRef<TradeEvent | null>(null);
  useEffect(() => {
    hoveredEventRef.current = hoveredEvent;
  }, [hoveredEvent]);
  const overlayFrameRef = useRef<number | null>(null);

  const drawOverlay = useCallback(() => {
    const canvas = overlayRef.current;
    const container = containerRef.current;
    const chart = chartRef.current;
    const series = candleRef.current;
    if (!canvas || !container || !chart || !series) return;
    const w = container.clientWidth;
    const h = container.clientHeight;
    const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const ts = chart.timeScale();
    const hovered = hoveredEventRef.current;
    for (const ev of tradeEventsRef.current) {
      const xRaw = ts.timeToCoordinate(ev.time as Time);
      const y = series.priceToCoordinate(ev.price);
      if (xRaw == null || y == null) continue;
      const x = xRaw + MARKER_X_OFFSET;
      const isHovered = hovered === ev;
      paintMarker(ctx, ev, x, y, isHovered);
    }
  }, []);

  const scheduleOverlayDraw = useCallback(() => {
    if (typeof window === "undefined") return;
    if (overlayFrameRef.current != null) return;
    overlayFrameRef.current = window.requestAnimationFrame(() => {
      overlayFrameRef.current = null;
      drawOverlay();
    });
  }, [drawOverlay]);

  // Init canvas overlay + listeners once the chart is up.
  useEffect(() => {
    const chart = chartRef.current;
    const container = containerRef.current;
    if (!chart || !container) return;

    const onRange = () => scheduleOverlayDraw();
    chart.timeScale().subscribeVisibleTimeRangeChange(onRange);

    const ro = new ResizeObserver(() => scheduleOverlayDraw());
    ro.observe(container);

    const onMouseMove = (e: MouseEvent) => {
      const rect = container.getBoundingClientRect();
      const px = e.clientX - rect.left;
      const py = e.clientY - rect.top;
      const series = candleRef.current;
      const ts = chart.timeScale();
      if (!series) {
        setHoveredEvent(null);
        setHoveredPos(null);
        container.style.cursor = "";
        return;
      }
      let best: { ev: TradeEvent; d: number; x: number; y: number } | null = null;
      for (const ev of tradeEventsRef.current) {
        const xRaw = ts.timeToCoordinate(ev.time as Time);
        const y = series.priceToCoordinate(ev.price);
        if (xRaw == null || y == null) continue;
        const x = xRaw + MARKER_X_OFFSET;
        const d = Math.hypot(px - x, py - y);
        if (d <= 16 && (!best || d < best.d)) {
          best = { ev, d, x, y };
        }
      }
      if (best) {
        setHoveredEvent(best.ev);
        setHoveredPos({ x: best.x, y: best.y });
        container.style.cursor = "pointer";
      } else {
        setHoveredEvent(null);
        setHoveredPos(null);
        container.style.cursor = "";
      }
    };
    const onMouseLeave = () => {
      setHoveredEvent(null);
      setHoveredPos(null);
      container.style.cursor = "";
    };
    container.addEventListener("mousemove", onMouseMove);
    container.addEventListener("mouseleave", onMouseLeave);

    scheduleOverlayDraw();

    return () => {
      chart.timeScale().unsubscribeVisibleTimeRangeChange(onRange);
      ro.disconnect();
      container.removeEventListener("mousemove", onMouseMove);
      container.removeEventListener("mouseleave", onMouseLeave);
      if (overlayFrameRef.current != null) {
        window.cancelAnimationFrame(overlayFrameRef.current);
        overlayFrameRef.current = null;
      }
    };
  }, [scheduleOverlayDraw]);

  // Redraw when the events list or hovered state changes.
  useEffect(() => {
    scheduleOverlayDraw();
  }, [tradeEvents, hoveredEvent, bars, scheduleOverlayDraw]);

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
      <canvas
        ref={overlayRef}
        className="chart-marker-overlay"
        style={{
          position: "absolute",
          inset: 0,
          pointerEvents: "none",
          zIndex: 5,
        }}
        aria-hidden
      />
      <ChartCrosshairTooltip data={tooltip} />
      <div style={{ position: "absolute", top: 6, right: 10, fontSize: 11, color: connected ? "var(--down)" : "var(--warn)" }}>
        {status}
      </div>
      {tooltip?.ohlc && (
        <HiLoBadge hi={tooltip.ohlc.high} lo={tooltip.ohlc.low} />
      )}
      <TradeMarkerTooltip
        event={hoveredEvent}
        x={hoveredPos?.x ?? null}
        y={hoveredPos?.y ?? null}
      />
    </div>
  );
}
