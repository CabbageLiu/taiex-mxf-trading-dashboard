"use client";

import { t } from "@/lib/i18n";

type Props = {
  hi: number;
  lo: number;
};

function fmtPrice(n: number): string {
  return n.toLocaleString("zh-Hant-TW", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function HiLoBadge({ hi, lo }: Props) {
  return (
    <aside className="hi-lo-badge" aria-live="polite">
      <div className="hi-lo-row">
        <span className="hi-lo-label">{t("hilo.high")}</span>
        <span className="hi-lo-num up">{fmtPrice(hi)}</span>
      </div>
      <div className="hi-lo-row">
        <span className="hi-lo-label">{t("hilo.low")}</span>
        <span className="hi-lo-num down">{fmtPrice(lo)}</span>
      </div>
    </aside>
  );
}
