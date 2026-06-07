"use client";

import { useEffect, useRef, useState } from "react";

export type WsMessage =
  | { type: "bar_update"; resolution: string; bucket: string; price: number; ts: string; symbol: string }
  | { type: "bar_close"; resolution: string; bucket: string; symbol: string }
  | {
      type: "signal";
      id: number | null;
      ts: string;
      symbol: string;
      resolution: string;
      strategy: string;
      side: string;
      price: number;
      reason: string;
      payload: Record<string, unknown>;
    }
  | {
      type: "trend_update";
      ts: string;
      symbol: string;
      label: string;
      score: number;
      ema20: number;
      ema50: number;
      plus_di: number;
      minus_di: number;
      adx: number;
      direction: -1 | 0 | 1;
    };

const wsBase = () => {
  if (typeof window === "undefined") return "";
  // Same-origin path. Works for localhost:3000 (Next dev proxies) AND for
  // Tailscale Serve where the page itself is reached via the tailnet hostname.
  // The Next.js rewrite rule in next.config.mjs forwards /ws/* to the FastAPI
  // backend so the browser only ever talks to the origin it loaded from.
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}`;
};

export function useStream(res: string, onMsg: (m: WsMessage) => void) {
  const cb = useRef(onMsg);
  cb.current = onMsg;
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let stopped = false;
    let ws: WebSocket | null = null;
    let retry = 0;

    function open() {
      if (stopped) return;
      ws = new WebSocket(`${wsBase()}/ws/stream?res=${encodeURIComponent(res)}`);
      ws.onopen = () => { retry = 0; setConnected(true); };
      ws.onclose = () => {
        setConnected(false);
        if (stopped) return;
        const wait = Math.min(1000 * 2 ** retry++, 15_000);
        setTimeout(open, wait);
      };
      ws.onerror = () => { try { ws?.close(); } catch {} };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as WsMessage;
          cb.current(msg);
        } catch {}
      };
    }
    open();
    return () => { stopped = true; ws?.close(); };
  }, [res]);

  return connected;
}
