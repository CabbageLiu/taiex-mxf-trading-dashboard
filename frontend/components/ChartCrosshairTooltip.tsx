"use client";

import { t } from "@/lib/i18n";

export type CrosshairOhlc = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
};

export type CrosshairIndicators = {
  ma?: { period: number; value: number };
  macd?: { macd: number; signal: number; hist: number };
  rsi?: { value: number };
  kd?: { k: number; d: number };
  dmi?: { plus: number; minus: number; adx: number };
};

export type CrosshairData = {
  ohlc: CrosshairOhlc | null;
  indicators: CrosshairIndicators;
  cursorPrice?: number | null;
};

type Props = {
  data: CrosshairData | null;
};

function fmtPrice(n: number): string { return n.toFixed(2); }
function fmtInd(n: number, dp = 1): string { return n.toFixed(dp); }
function signed(n: number, dp = 1): string {
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(dp)}`;
}

function fmtTime(epoch: number): string {
  // lightweight-charts gives us a UTC epoch in seconds — render in Asia/Taipei.
  try {
    return new Date(epoch * 1000).toLocaleString("zh-Hant-TW", {
      timeZone: "Asia/Taipei",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return String(epoch);
  }
}

/**
 * Side-anchored panel rendered above the chart container. Receives
 * already-computed values from the Chart parent — does no fetching.
 */
export function ChartCrosshairTooltip({ data }: Props) {
  if (!data) return null;
  if (data.cursorPrice == null && !data.ohlc) return null;
  const { ohlc, indicators } = data;
  const dir = ohlc ? (ohlc.close >= ohlc.open ? "up" : "down") : "";
  return (
    <div className="crosshair-tooltip" role="status" aria-live="off">
      <dl>
        {data.cursorPrice != null && (
          <>
            <dt>{t("crosshair.cursor")}</dt>
            <dd className="v">{fmtPrice(data.cursorPrice)}</dd>
          </>
        )}
        {ohlc && (
          <>
            <dt>{t("crosshair.time")}</dt>
            <dd className="v">{fmtTime(ohlc.time)}</dd>
            <dt>{t("crosshair.ohlc")}</dt>
            <dd className={`v ${dir}`}>
              {fmtPrice(ohlc.open)} / {fmtPrice(ohlc.high)} / {fmtPrice(ohlc.low)} / {fmtPrice(ohlc.close)}
            </dd>
          </>
        )}
        {indicators.ma && (
          <>
            <dt>MA{indicators.ma.period}</dt>
            <dd className="v">{fmtPrice(indicators.ma.value)}</dd>
          </>
        )}
        {indicators.macd && (
          <>
            <dt>MACD</dt>
            <dd className="v">
              {signed(indicators.macd.macd)} / {signed(indicators.macd.signal)} / {signed(indicators.macd.hist)}
            </dd>
          </>
        )}
        {indicators.kd && (
          <>
            <dt>KD</dt>
            <dd className="v">
              K {fmtInd(indicators.kd.k)}　D {fmtInd(indicators.kd.d)}
            </dd>
          </>
        )}
        {indicators.rsi && (
          <>
            <dt>RSI</dt>
            <dd className="v">{fmtInd(indicators.rsi.value)}</dd>
          </>
        )}
        {indicators.dmi && (
          <>
            <dt>DMI</dt>
            <dd className="v">
              +DI {fmtInd(indicators.dmi.plus, 0)}　−DI {fmtInd(indicators.dmi.minus, 0)}　ADX {fmtInd(indicators.dmi.adx, 0)}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}
