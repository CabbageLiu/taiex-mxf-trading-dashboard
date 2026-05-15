"use client";

const RES = ["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w", "1mo"] as const;
export type Resolution = (typeof RES)[number];

export function ResolutionSelector({
  value,
  onChange,
}: {
  value: Resolution;
  onChange: (r: Resolution) => void;
}) {
  return (
    <div style={{ display: "flex", gap: 4 }}>
      {RES.map((r) => (
        <button
          key={r}
          className="btn"
          aria-pressed={value === r}
          onClick={() => onChange(r)}
        >
          {r}
        </button>
      ))}
    </div>
  );
}

export const RESOLUTIONS = RES;
