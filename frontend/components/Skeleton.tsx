import { CSSProperties } from "react";

type Props = {
  width?: number | string;
  height?: number | string;
  radius?: number | string;
  style?: CSSProperties;
};

export function Skeleton({ width = "100%", height = 14, radius = 2, style }: Props) {
  return (
    <span
      className="skeleton"
      aria-hidden
      style={{
        display: "inline-block",
        width,
        height,
        borderRadius: radius,
        background:
          "linear-gradient(90deg, rgba(168,119,61,0.06) 0%, rgba(168,119,61,0.16) 50%, rgba(168,119,61,0.06) 100%)",
        backgroundSize: "200% 100%",
        animation: "shimmer 1.4s linear infinite",
        verticalAlign: "middle",
        ...style,
      }}
    />
  );
}
