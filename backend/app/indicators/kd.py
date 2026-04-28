from __future__ import annotations

import pandas as pd


class KD:
    """Stochastic %K / %D — TW-style smoothing (3-period EMA on %K → %D)."""

    name = "kd"

    def compute(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        n = int(params.get("period", 9))
        k_smooth = int(params.get("k_smooth", 3))
        d_smooth = int(params.get("d_smooth", 3))

        low_n = bars["low"].rolling(n, min_periods=n).min()
        high_n = bars["high"].rolling(n, min_periods=n).max()
        rsv = (bars["close"] - low_n) / (high_n - low_n).replace(0, pd.NA) * 100
        k = rsv.ewm(alpha=1 / k_smooth, adjust=False).mean()
        d = k.ewm(alpha=1 / d_smooth, adjust=False).mean()
        return pd.DataFrame({"k": k, "d": d}, index=bars.index)
