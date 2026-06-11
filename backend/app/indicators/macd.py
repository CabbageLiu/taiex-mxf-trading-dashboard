from __future__ import annotations

import pandas as pd


class MACD:
    name = "macd"

    def compute(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        fast = int(params.get("fast", 12))
        slow = int(params.get("slow", 26))
        signal = int(params.get("signal", 9))

        ema_fast = bars["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = bars["close"].ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        sig = macd.ewm(span=signal, adjust=False).mean()
        hist = macd - sig
        return pd.DataFrame(
            {"macd": macd, "signal": sig, "hist": hist}, index=bars.index
        )
