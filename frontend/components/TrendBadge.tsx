"use client";

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { TrendSnapshot } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useTrend } from "@/lib/queries";
import { useStream, type WsMessage } from "@/lib/ws";

// Floor used to bucket the score into a colour tone. Mirrors the rough
// neutral band of the 5-band Chinese legend so the colour and label move
// in lock-step at the 盤整 boundary.
const TONE_THRESHOLD = 0.1;

export function TrendBadge() {
  const trendQ = useTrend();
  const qc = useQueryClient();

  // WS push is the primary update path. The hook-shaped `useStream(res, onMsg)`
  // contract requires a resolution arg even though `trend_update` is a
  // broadcast — pass "15m" since the trend tracker runs on 15m closes.
  const onMsg = useCallback(
    (msg: WsMessage) => {
      if (msg.type !== "trend_update") return;
      const next: TrendSnapshot = {
        ts: msg.ts,
        symbol: msg.symbol,
        resolution: "15m",
        ema20: msg.ema20,
        ema50: msg.ema50,
        plus_di: msg.plus_di,
        minus_di: msg.minus_di,
        adx: msg.adx,
        direction: msg.direction,
        score: msg.score,
        label: msg.label,
      };
      qc.setQueryData<TrendSnapshot | null>(["trend"], next);
    },
    [qc],
  );
  useStream("15m", onMsg);

  const snap = trendQ.data;
  const score = snap?.score;
  const tone =
    score == null
      ? "muted"
      : score > TONE_THRESHOLD
        ? "up"
        : score < -TONE_THRESHOLD
          ? "down"
          : "muted";

  return (
    <aside className="trend-badge" aria-live="polite">
      <div className="trend-row">
        <span className="trend-key">{t("trend.label")}</span>
        <span className="trend-val">{snap?.label ?? "—"}</span>
      </div>
      <div className="trend-row">
        <span className="trend-key">{t("trend.score")}</span>
        <span className={`trend-val mono trend-tone-${tone}`}>
          {score == null ? "—" : (score >= 0 ? "+" : "") + score.toFixed(2)}
        </span>
      </div>
      <details className="trend-legend">
        <summary>{t("trend.legend.title")}</summary>
        <dl>
          <dt className="mono">&gt; +0.70</dt>
          <dd>{t("trend.strongUp")}</dd>
          <dt className="mono">+0.30 ~ +0.70</dt>
          <dd>{t("trend.gentleUp")}</dd>
          <dt className="mono">-0.30 ~ +0.30</dt>
          <dd>{t("trend.neutral")}</dd>
          <dt className="mono">-0.70 ~ -0.30</dt>
          <dd>{t("trend.gentleDown")}</dd>
          <dt className="mono">&lt; -0.70</dt>
          <dd>{t("trend.strongDown")}</dd>
        </dl>
      </details>
    </aside>
  );
}
