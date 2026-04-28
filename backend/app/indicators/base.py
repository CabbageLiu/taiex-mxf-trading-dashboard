from __future__ import annotations

from typing import Protocol

import pandas as pd


class Indicator(Protocol):
    name: str

    def compute(self, bars: pd.DataFrame, params: dict) -> pd.DataFrame: ...
