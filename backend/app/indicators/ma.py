from __future__ import annotations

import pandas as pd


class MA:
    name = "ma"

    def compute(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        period = int(params.get("period", 20))
        kind = str(params.get("kind", "sma")).lower()
        if kind == "ema":
            ma = bars["close"].ewm(span=period, adjust=False).mean()
        else:
            ma = bars["close"].rolling(window=period, min_periods=period).mean()
        out = pd.DataFrame({"ma": ma}, index=bars.index)
        return out
