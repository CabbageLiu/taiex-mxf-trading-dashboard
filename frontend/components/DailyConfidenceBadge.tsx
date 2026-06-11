"use client";

import { useStrategyState } from "@/lib/queries";

type Props = {
  strategy: string | null | undefined;
};

function Dots({ score, color }: { score: number; color: string }) {
  return (
    <span className="conf-dots" aria-label={`${score} of 3`}>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="conf-dot"
          style={{
            background: i < score ? color : "transparent",
            borderColor: color,
          }}
        />
      ))}
    </span>
  );
}

export function DailyConfidenceBadge({ strategy }: Props) {
  const q = useStrategyState(strategy ?? null);
  if (!strategy) return null;
  if (!q.data) return null;
  const s = q.data.state ?? {};
  const longScore = s.daily_confidence_long ?? 0;
  const shortScore = s.daily_confidence_short ?? 0;
  const pos = s.position ?? null;
  const cooldown = s.cooldown_left ?? 0;

  // Hide entirely if the strategy has no recorded activity yet (cold start).
  if (
    longScore === 0 &&
    shortScore === 0 &&
    pos == null &&
    cooldown === 0 &&
    !s.daily_last_bucket
  ) {
    return null;
  }

  const positionLine = pos
    ? `持有 ${pos.side} @ ${pos.entry_price.toFixed(0)}`
    : cooldown > 0
      ? `空手 · 冷卻 ${cooldown} 根`
      : "空手";

  return (
    <aside className="confidence-badge" aria-live="polite">
      <div className="conf-row">
        <span className="conf-label">多</span>
        <Dots score={longScore} color="var(--up)" />
        <span className="conf-num">{longScore}/3</span>
      </div>
      <div className="conf-row">
        <span className="conf-label">空</span>
        <Dots score={shortScore} color="var(--down)" />
        <span className="conf-num">{shortScore}/3</span>
      </div>
      <div className="conf-pos">{positionLine}</div>
    </aside>
  );
}
