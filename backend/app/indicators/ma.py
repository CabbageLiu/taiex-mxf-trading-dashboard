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
        # Surface the underlying close alongside the MA so consumers that need
        # both (e.g. strat_1k's ``above_ema20`` 5m alignment gate) can read a
        # single aux frame without a second lookup. Additive; existing readers
        # of the ``ma`` column are unaffected.
        out = pd.DataFrame({"close": bars["close"], "ma": ma}, index=bars.index)
        return out
