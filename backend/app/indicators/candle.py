from __future__ import annotations

import pandas as pd


class CandleDirection:
    """Per-bar direction: +1 green (close>open), -1 red (close<open), 0 doji.

    No params. Aligned to ``bars.index``. Designed as a lightweight aux
    indicator so strategies can gate on multi-bar candlestick patterns
    (e.g. V-rebound) at non-primary resolutions via ``aux_indicator_specs``.
    """

    name = "candle_direction"

    def compute(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame:
        diff = bars["close"].astype(float) - bars["open"].astype(float)
        direction = diff.apply(lambda v: 1 if v > 0 else (-1 if v < 0 else 0))
        return pd.DataFrame({"direction": direction.astype("int64")}, index=bars.index)
