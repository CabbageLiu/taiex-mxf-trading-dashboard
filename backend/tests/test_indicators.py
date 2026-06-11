import numpy as np
import pandas as pd
import pytest

from app.indicators.service import compute, available


@pytest.fixture
def bars() -> pd.DataFrame:
    n = 60
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    close = pd.Series(np.linspace(100, 160, n), index=idx)  # straight uptrend
    return pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5, "close": close}
    )


def test_available_lists_registry():
    assert set(available()) == {"ma", "macd", "rsi", "kd", "dmi", "atr", "candle_direction"}


def test_ma_sma_default_period_20(bars):
    df = compute("ma", bars, {})
    assert df["ma"].iloc[-1] == pytest.approx(bars["close"].iloc[-20:].mean())


def test_ma_ema_kind(bars):
    df = compute("ma", bars, {"period": 5, "kind": "ema"})
    expected = bars["close"].ewm(span=5, adjust=False).mean().iloc[-1]
    assert df["ma"].iloc[-1] == pytest.approx(expected)


def test_ma_output_includes_close(bars):
    """MA frame must surface the underlying ``close`` so consumers needing
    both close and the moving average (e.g. a close-vs-MA alignment gate on
    an aux resolution) can read a single aux frame. Additive contract.
    """
    df = compute("ma", bars, {"period": 5, "kind": "ema"})
    assert "close" in df.columns
    assert df["close"].iloc[-1] == pytest.approx(bars["close"].iloc[-1])


def test_macd_columns_and_hist_definition(bars):
    df = compute("macd", bars, {})
    assert list(df.columns) == ["macd", "signal", "hist"]
    last = df.iloc[-1]
    assert last["hist"] == pytest.approx(last["macd"] - last["signal"])


def test_rsi_strong_uptrend_close_to_100(bars):
    df = compute("rsi", bars, {"period": 14})
    assert df["rsi"].iloc[-1] > 95


def test_kd_returns_k_d_and_bounded(bars):
    df = compute("kd", bars, {})
    last = df.iloc[-1]
    assert 0 <= last["k"] <= 100
    assert 0 <= last["d"] <= 100


def test_dmi_uptrend_plus_di_dominates(bars):
    df = compute("dmi", bars, {})
    last = df.iloc[-1]
    assert last["plus_di"] > last["minus_di"]
    assert last["adx"] > 50


def test_unknown_indicator_raises(bars):
    import pytest as p

    with p.raises(KeyError):
        compute("nope", bars, {})
