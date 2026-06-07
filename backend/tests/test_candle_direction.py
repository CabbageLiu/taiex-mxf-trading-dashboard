from __future__ import annotations

import pandas as pd
import pytest

from app.indicators.service import compute


def _bars(rows: list[tuple[float, float]]) -> pd.DataFrame:
    idx = pd.date_range("2026-05-20 09:00", periods=len(rows), freq="5min", tz="UTC")
    opens = [r[0] for r in rows]
    closes = [r[1] for r in rows]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) + 1 for o, c in rows],
            "low": [min(o, c) - 1 for o, c in rows],
            "close": closes,
        },
        index=idx,
    )


def test_direction_green_red_doji_mapping():
    bars = _bars([
        (100.0, 105.0),  # green
        (105.0, 100.0),  # red
        (100.0, 100.0),  # doji
        (100.0, 100.5),  # green (small body)
        (100.0, 99.5),   # red
    ])
    df = compute("candle_direction", bars, {})
    assert list(df["direction"]) == [1, -1, 0, 1, -1]


def test_index_and_length_preserved():
    bars = _bars([(100.0, 101.0)] * 7)
    df = compute("candle_direction", bars, {})
    assert len(df) == 7
    assert list(df.index) == list(bars.index)
    assert list(df.columns) == ["direction"]


def test_no_params_required():
    bars = _bars([(100.0, 101.0), (101.0, 100.5)])
    df_empty = compute("candle_direction", bars, {})
    df_any = compute("candle_direction", bars, {"ignored": 99})
    assert list(df_empty["direction"]) == list(df_any["direction"])


def test_doji_floats_with_tiny_noise_are_not_doji():
    # Tie-break by strict inequality on float diff; only exact equality is 0.
    bars = _bars([(100.0, 100.0 + 1e-9)])
    df = compute("candle_direction", bars, {})
    assert df["direction"].iloc[0] == 1


def test_dtype_is_integer():
    bars = _bars([(100.0, 101.0), (101.0, 100.0), (100.0, 100.0)])
    df = compute("candle_direction", bars, {})
    assert pd.api.types.is_integer_dtype(df["direction"])


@pytest.mark.parametrize(
    "open_, close_, expected",
    [
        (100.0, 100.0, 0),
        (100.5, 100.5, 0),
        (100.0, 100.01, 1),
        (100.01, 100.0, -1),
    ],
)
def test_edge_cases(open_, close_, expected):
    bars = _bars([(open_, close_)])
    df = compute("candle_direction", bars, {})
    assert df["direction"].iloc[0] == expected
