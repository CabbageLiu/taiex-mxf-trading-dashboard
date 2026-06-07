from __future__ import annotations

import pandas as pd


class ATR:
    """Wilder ATR. Returns one column: atr."""

    name = "atr"

    def compute(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        n = int(params.get("period", 14))

        high = bars["high"]
        low = bars["low"]
        prev_close = bars["close"].shift(1)
        tr = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        atr = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        return pd.DataFrame({"atr": atr}, index=bars.index)
