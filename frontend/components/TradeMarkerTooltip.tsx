"use client";

import type { TradeEvent } from "./Chart";

type Props = {
  event: TradeEvent | null;
  x: number | null;
  y: number | null;
};

const STRATEGY_HEX: Record<string, string> = {
  trade_strat_v1: "#1e88e5",
  trade_strat_v2: "#fb8c00",
};

function strategyHex(name: string): string {
  return STRATEGY_HEX[name] ?? "#8a8175";
}

function sideLabel(side: string): string {
  const u = side?.toUpperCase();
  if (u === "LONG") return "多";
  if (u === "SHORT") return "空";
  return side;
}

function sourceLabel(src: "LIVE" | "BACKTEST"): string {
  return src === "LIVE" ? "實盤" : "回測";
}

function fmtPrice(n: number): string {
  return n.toLocaleString("zh-Hant-TW", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function fmtPnl(n: number): string {
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)}`;
}

/**
 * Floating card anchored near a hovered trade marker. Shape grammar:
 *   - OPEN  → kind chip in strategy color
 *   - CLOSE → kind chip + win/loss PnL pill
 * The Chart component computes pixel coords from priceToCoordinate /
 * timeToCoordinate and passes them in via x/y so the card hugs the dot.
 */
export function TradeMarkerTooltip({ event, x, y }: Props) {
  if (!event || x == null || y == null) return null;
  const isClose = event.kind === "CLOSE";
  const isWin = isClose && (event.pnl ?? 0) >= 0;
  const stratColor = strategyHex(event.strategy);
  const kindLabel = event.kind === "OPEN" ? "開倉" : "關倉";

  // Anchor the card 16 px above the marker; let CSS translate(-50%) center it
  // horizontally on the dot. Caller is expected to render us as a child of
  // the relatively-positioned chart container so left/top resolve correctly.
  const style: React.CSSProperties = {
    position: "absolute",
    left: x,
    top: y - 16,
    transform: "translate(-50%, -100%)",
    pointerEvents: "none",
    zIndex: 12,
  };

  return (
    <div className="trade-marker-card" style={style} role="tooltip" aria-live="polite">
      <div className="trade-marker-head">
        <span
          className="trade-marker-kind"
          style={{
            background: stratColor,
            color: "#fff",
          }}
        >
          {kindLabel} · {sideLabel(event.side)}
        </span>
        <span className="trade-marker-strategy">{event.strategy}</span>
        <span className="trade-marker-source">{sourceLabel(event.source)}</span>
      </div>

      <div className="trade-marker-price">
        <span className="trade-marker-label">價格</span>
        <span className="trade-marker-num">{fmtPrice(event.price)}</span>
      </div>

      {isClose && event.pnl != null && (
        <div className={`trade-marker-pnl ${isWin ? "win" : "loss"}`}>
          {fmtPnl(event.pnl)} <span className="trade-marker-unit">點</span>
        </div>
      )}

      {event.reason && (
        <div className="trade-marker-reason">{event.reason}</div>
      )}
    </div>
  );
}
