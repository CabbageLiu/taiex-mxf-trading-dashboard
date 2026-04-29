"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";

import { Chart, type IndicatorState } from "@/components/Chart";
import { TopBar } from "@/components/TopBar";
import { type Resolution } from "@/components/ResolutionSelector";
import { AlertLog, type SignalRow } from "@/components/AlertLog";
import { api } from "@/lib/api";

const DEFAULT_INDICATORS: IndicatorState = {
  ma: { enabled: true, period: 20, kind: "sma" },
  macd: { enabled: true },
  rsi: { enabled: false, period: 14 },
  kd: { enabled: false },
  dmi: { enabled: false },
};

function TradingPageInner() {
  const sp = useSearchParams();
  const strategy = sp.get("s");
  const [res, setRes] = useState<Resolution>("1m");
  const [ind, setInd] = useState<IndicatorState>(DEFAULT_INDICATORS);
  const [signals, setSignals] = useState<SignalRow[]>([]);
  const qc = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["bars", res] }),
        qc.invalidateQueries({ queryKey: ["indicators"] }),
      ]);
    } finally {
      setRefreshing(false);
    }
  };

  const barsQ = useQuery({
    queryKey: ["bars", res],
    queryFn: () => api.bars({ res, limit: 500 }),
    // Refetch is a recovery mechanism (catch missed WS messages, recover
    // after sleep). The WS is the primary update path for the in-progress
    // bar; refetching every 30 s caused a visible reset of the live candle.
    refetchInterval: 300_000,
  });

  const enabledKinds = useMemo(() => {
    const k: string[] = [];
    if (ind.ma.enabled) k.push("ma");
    if (ind.macd.enabled) k.push("macd");
    if (ind.rsi.enabled) k.push("rsi");
    if (ind.kd.enabled) k.push("kd");
    if (ind.dmi.enabled) k.push("dmi");
    return k;
  }, [ind]);

  const indicatorParams = useMemo(() => [
    { kind: "ma", params: { period: ind.ma.period, kind: ind.ma.kind } },
    { kind: "rsi", params: { period: ind.rsi.period } },
  ], [ind.ma.period, ind.ma.kind, ind.rsi.period]);

  const indQ = useQuery({
    queryKey: ["indicators", res, enabledKinds, indicatorParams],
    queryFn: () => api.indicators({ res, kinds: enabledKinds, paramSpecs: indicatorParams }),
    enabled: enabledKinds.length > 0,
    // See note on barsQ — refetch is recovery only, not the primary path.
    refetchInterval: 300_000,
  });

  return (
    <div className="trading-grid">
      <div className="trading-main">
        <TopBar
          resolution={res}
          onResolutionChange={setRes}
          indicators={ind}
          onIndicatorsChange={setInd}
          onRefresh={handleRefresh}
          isRefreshing={refreshing}
        />
        <div style={{ flex: 1, minHeight: 0 }}>
          <Chart
            res={res}
            bars={barsQ.data?.bars ?? []}
            indicators={indQ.data?.series ?? {}}
            state={ind}
            strategy={strategy}
            onSignal={(m) => {
              if (m.type !== "signal") return;
              setSignals((prev) => [...prev, {
                ts: m.ts, symbol: m.symbol, resolution: m.resolution,
                strategy: m.strategy, side: m.side, price: m.price, reason: m.reason,
              }]);
            }}
          />
        </div>
      </div>

      <aside className="trading-side">
        <AlertLog liveSignals={signals} />
      </aside>
    </div>
  );
}

export default function TradingPage() {
  return (
    <Suspense fallback={null}>
      <TradingPageInner />
    </Suspense>
  );
}
