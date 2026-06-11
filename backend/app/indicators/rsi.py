from __future__ import annotations

import numpy as np
import pandas as pd


class RSI:
    name = "rsi"

    def compute(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        period = int(params.get("period", 14))
        delta = bars["close"].diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        # Wilder smoothing == EMA with alpha = 1/period
        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        # Pin RSI to 100 when there are no losses but gains exist (RS = inf).
        rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
        return pd.DataFrame({"rsi": rsi}, index=bars.index)
