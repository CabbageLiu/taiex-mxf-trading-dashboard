"use client";

import { useEffect, useMemo, useRef } from "react";
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type LineData,
  type HistogramData,
  type Time,
} from "lightweight-charts";
import type { Bar } from "@/lib/api";
import { useStream, type WsMessage } from "@/lib/ws";
import { t } from "@/lib/i18n";

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

export function Chart({ res, bars, indicators, state, onSignal }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const maRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdLineRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdSigRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdHistRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const rsiRef = useRef<ISeriesApi<"Line"> | null>(null);
  const kRef = useRef<ISeriesApi<"Line"> | null>(null);
  const dRef = useRef<ISeriesApi<"Line"> | null>(null);
  const plusDIRef = useRef<ISeriesApi<"Line"> | null>(null);
  const minusDIRef = useRef<ISeriesApi<"Line"> | null>(null);
  const adxRef = useRef<ISeriesApi<"Line"> | null>(null);

  // Initialise chart
  useEffect(() => {
    if (!containerRef.current) return;
    // TW market convention: red = up 漲, green = down 跌
    const UP = "#c0392b";
    const DOWN = "#3a7d4f";
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { color: "#fbf7ee" }, textColor: "#2a2a2a" },
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
    return () => { chart.remove(); chartRef.current = null; };
  }, []);

  // Push bars
  useEffect(() => {
    if (!candleRef.current) return;
    const data: CandlestickData<Time>[] = bars.map((b) => ({
      time: b.time as Time,
      open: b.open, high: b.high, low: b.low, close: b.close,
    }));
    candleRef.current.setData(data);
  }, [bars]);

  const wantMA = state.ma.enabled && !!indicators.ma;
  const wantMACD = state.macd.enabled && !!indicators.macd;
  const wantRSI = state.rsi.enabled && !!indicators.rsi;
  const wantKD = state.kd.enabled && !!indicators.kd;
  const wantDMI = state.dmi.enabled && !!indicators.dmi;

  // MA overlay on price pane
  useEffect(() => {
    if (!chartRef.current) return;
    if (!wantMA) {
      if (maRef.current) { chartRef.current.removeSeries(maRef.current); maRef.current = null; }
      return;
    }
    if (!maRef.current) maRef.current = chartRef.current.addSeries(LineSeries, { color: "#7a5a3a", lineWidth: 1 });
    const data: LineData<Time>[] = (indicators.ma ?? [])
      .filter((p) => p.ma != null)
      .map((p) => ({ time: p.time as Time, value: p.ma as number }));
    maRef.current.setData(data);
  }, [indicators.ma, wantMA]);

  // MACD as separate price scale
  useEffect(() => {
    if (!chartRef.current) return;
    const chart = chartRef.current;
    const cleanup = () => {
      [macdLineRef, macdSigRef, macdHistRef].forEach((r) => {
        if (r.current) { chart.removeSeries(r.current); r.current = null; }
      });
    };
    if (!wantMACD) { cleanup(); return; }
    const opts = { priceScaleId: "macd", priceFormat: { type: "price", precision: 2, minMove: 0.01 } } as const;
    if (!macdHistRef.current) macdHistRef.current = chart.addSeries(HistogramSeries, { ...opts, color: "#8a8175" });
    if (!macdLineRef.current) macdLineRef.current = chart.addSeries(LineSeries, { ...opts, color: "#2a2a2a", lineWidth: 1 });
    if (!macdSigRef.current) macdSigRef.current = chart.addSeries(LineSeries, { ...opts, color: "#7a5a3a", lineWidth: 1 });
    chart.priceScale("macd").applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });
    const rows = indicators.macd ?? [];
    const histData: HistogramData<Time>[] = rows
      .filter((p) => p.hist != null)
      .map((p) => ({ time: p.time as Time, value: p.hist as number, color: (p.hist as number) >= 0 ? "#c0392b55" : "#3a7d4f55" }));
    const lineData: LineData<Time>[] = rows.filter((p) => p.macd != null).map((p) => ({ time: p.time as Time, value: p.macd as number }));
    const sigData: LineData<Time>[] = rows.filter((p) => p.signal != null).map((p) => ({ time: p.time as Time, value: p.signal as number }));
    macdHistRef.current!.setData(histData);
    macdLineRef.current!.setData(lineData);
    macdSigRef.current!.setData(sigData);
  }, [indicators.macd, wantMACD]);

  // RSI on its own scale
  useEffect(() => {
    if (!chartRef.current) return;
    const chart = chartRef.current;
    if (!wantRSI) {
      if (rsiRef.current) { chart.removeSeries(rsiRef.current); rsiRef.current = null; }
      return;
    }
    if (!rsiRef.current) rsiRef.current = chart.addSeries(LineSeries, { priceScaleId: "rsi", color: "#5d3f6e", lineWidth: 1 });
    chart.priceScale("rsi").applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });
    const rows = (indicators.rsi ?? []).filter((p) => p.rsi != null);
    rsiRef.current.setData(rows.map((p) => ({ time: p.time as Time, value: p.rsi as number })));
  }, [indicators.rsi, wantRSI]);

  // KD on its own scale
  useEffect(() => {
    if (!chartRef.current) return;
    const chart = chartRef.current;
    if (!wantKD) {
      if (kRef.current) { chart.removeSeries(kRef.current); kRef.current = null; }
      if (dRef.current) { chart.removeSeries(dRef.current); dRef.current = null; }
      return;
    }
    if (!kRef.current) kRef.current = chart.addSeries(LineSeries, { priceScaleId: "kd", color: "#2a6f5a", lineWidth: 1 });
    if (!dRef.current) dRef.current = chart.addSeries(LineSeries, { priceScaleId: "kd", color: "#a4793a", lineWidth: 1 });
    chart.priceScale("kd").applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });
    const rows = indicators.kd ?? [];
    kRef.current.setData(rows.filter((p) => p.k != null).map((p) => ({ time: p.time as Time, value: p.k as number })));
    dRef.current.setData(rows.filter((p) => p.d != null).map((p) => ({ time: p.time as Time, value: p.d as number })));
  }, [indicators.kd, wantKD]);

  // DMI on its own scale
  useEffect(() => {
    if (!chartRef.current) return;
    const chart = chartRef.current;
    if (!wantDMI) {
      [plusDIRef, minusDIRef, adxRef].forEach((r) => {
        if (r.current) { chart.removeSeries(r.current); r.current = null; }
      });
      return;
    }
    // +DI = up = 紅; −DI = down = 綠 (TW convention); ADX = ink-brown
    if (!plusDIRef.current) plusDIRef.current = chart.addSeries(LineSeries, { priceScaleId: "dmi", color: "#c0392b", lineWidth: 1 });
    if (!minusDIRef.current) minusDIRef.current = chart.addSeries(LineSeries, { priceScaleId: "dmi", color: "#3a7d4f", lineWidth: 1 });
    if (!adxRef.current) adxRef.current = chart.addSeries(LineSeries, { priceScaleId: "dmi", color: "#7a5a3a", lineWidth: 1 });
    chart.priceScale("dmi").applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });
    const rows = indicators.dmi ?? [];
    plusDIRef.current.setData(rows.filter((p) => p.plus_di != null).map((p) => ({ time: p.time as Time, value: p.plus_di as number })));
    minusDIRef.current.setData(rows.filter((p) => p.minus_di != null).map((p) => ({ time: p.time as Time, value: p.minus_di as number })));
    adxRef.current.setData(rows.filter((p) => p.adx != null).map((p) => ({ time: p.time as Time, value: p.adx as number })));
  }, [indicators.dmi, wantDMI]);

  // Live updates from WS
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
    } else {
      last.high = Math.max(last.high, price);
      last.low = Math.min(last.low, price);
      last.close = price;
      candleRef.current.update({ time: ts as Time, open: last.open, high: last.high, low: last.low, close: last.close });
    }
  });

  const status = useMemo(() => connected ? t("status_live") : t("status_reconnecting"), [connected]);

  return (
    <div style={{ position: "relative", height: "100%" }}>
      <div ref={containerRef} className="chart" style={{ height: "100%" }} />
      <div style={{ position: "absolute", top: 6, right: 10, fontSize: 11, color: connected ? "var(--down)" : "#a4793a" }}>
        {status}
      </div>
    </div>
  );
}
