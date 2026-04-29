"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  type IChartApi,
  type IPaneApi,
  type ISeriesApi,
  type CandlestickData,
  type LineData,
  type HistogramData,
  type MouseEventParams,
  type Time,
} from "lightweight-charts";
import type { Bar } from "@/lib/api";
import { useStream, type WsMessage } from "@/lib/ws";
import { t } from "@/lib/i18n";
import {
  ChartCrosshairTooltip,
  type CrosshairData,
  type CrosshairIndicators,
} from "./ChartCrosshairTooltip";

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
};

// TW market convention: red = up 漲, green = down 跌
const UP = "#c0392b";
const DOWN = "#3a7d4f";
const INK = "#1f1d1a";
const ACCENT = "#a8773d";

type IndKey = "macd" | "rsi" | "kd" | "dmi";

type PaneRefs = {
  pane: IPaneApi<Time> | null;
  series: ISeriesApi<any>[];
};

export function Chart({ res, bars, indicators, state, onSignal }: Props) {
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
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#e3dccf" },
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
      if (!param.point || param.time == null) {
        setTooltip(null);
        return;
      }
      const time = Number(param.time);
      const bar = lookups.current.bars.get(time);
      if (!bar) {
        setTooltip(null);
        return;
      }
      const ind: CrosshairIndicators = {};
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
      setTooltip({ ohlc: { time, ...bar }, indicators: ind });
    };
    chart.subscribeCrosshairMove(onMove);

    return () => {
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      maRef.current = null;
      macdRef.current = null;
      rsiRef.current = null;
      kdRef.current = null;
      dmiRef.current = null;
    };
  }, []);

  // ─── Push bars ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (!candleRef.current) return;
    const data: CandlestickData<Time>[] = bars.map((b) => ({
      time: b.time as Time,
      open: b.open, high: b.high, low: b.low, close: b.close,
    }));
    candleRef.current.setData(data);

    const map = new Map<number, { open: number; high: number; low: number; close: number }>();
    for (const b of bars) map.set(b.time, { open: b.open, high: b.high, low: b.low, close: b.close });
    lookups.current.bars = map;
  }, [bars]);

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

  // ─── Live updates from WS ─────────────────────────────────────────────
  const lastBarRef = useRef<{ time: number; open: number; high: number; low: number; close: number } | null>(null);
  useEffect(() => {
    lastBarRef.current = bars.length ? { ...bars[bars.length - 1] } : null;
  }, [bars]);

  const connected = useStream(res, (m) => {
    if (m.type === "signal") { onSignal?.(m); return; }
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
    </div>
  );
}
