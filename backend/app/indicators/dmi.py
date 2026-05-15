from __future__ import annotations

import numpy as np
import pandas as pd


class DMI:
    """Wilder DMI/ADX. Returns +DI, -DI, ADX."""

    name = "dmi"

    def compute(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        n = int(params.get("period", 14))

        high = bars["high"]
        low = bars["low"]
        close = bars["close"]

        up = high.diff()
        dn = -low.diff()
        plus_dm = ((up > dn) & (up > 0)).astype(float) * up
        minus_dm = ((dn > up) & (dn > 0)).astype(float) * dn

        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        # Wilder smoothing == EMA with alpha = 1/n
        atr = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr
        minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr
        dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
        adx = dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()

        return pd.DataFrame(
            {"plus_di": plus_di, "minus_di": minus_di, "adx": adx}, index=bars.index
        )
