"""Phase A enrichment: compute 3 pre-registered 5m alignment signals per trade.

For each trade in /tmp/strat_1k_trade_dump.json, fetch 5m bars in
[entry_ts - 3h, entry_ts], close-trim to buckets where bucket_end <= entry_ts,
then compute:
    f1_5m_macd_hist     — MACD hist (12/26/9), last row
    f2_5m_di_diff       — +DI - -DI (period=14), last row
    f3_5m_close_minus_ema20 — close - EMA20 (period=20), last row

Saves enriched dump to /tmp/strat_1k_trade_5m_enriched.json and prints
W-vs-L and filter-implication tables to stdout.
"""
from __future__ import annotations

import asyncio
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.api.routes.bars import load_bars
from app.db.engine import dispose_engine, init_engine
from app.indicators.dmi import DMI
from app.indicators.ma import MA
from app.indicators.macd import MACD

TRADE_DUMP_PATH = Path("/tmp/strat_1k_trade_dump.json")
ENRICHED_PATH = Path("/tmp/strat_1k_trade_5m_enriched.json")
SYMBOL = "MXF"
RES = "5m"
LOOKBACK = timedelta(hours=12)  # wide enough to cross session gaps (05:00–08:45 TPE)
BUCKET_DELTA = timedelta(minutes=5)

MACD_PARAMS = {"fast": 12, "slow": 26, "signal": 9}
DMI_PARAMS = {"period": 14}
EMA20_PARAMS = {"period": 20, "kind": "ema"}


def _parse_ts(s: str) -> datetime:
    # JSON dump uses ISO8601Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


async def _fetch_global_window(trades: list[dict]) -> pd.DataFrame:
    """One DB call covering all trades, used for slicing per trade."""
    entries = [_parse_ts(t["entry_ts"]) for t in trades]
    start = min(entries) - LOOKBACK - timedelta(minutes=10)
    end = max(entries) + timedelta(minutes=5)
    df = await load_bars(SYMBOL, RES, start=start, end=end)
    if df.empty:
        return df
    # Ensure index is tz-aware UTC for clean comparisons
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def _enrich_trade(trade: dict, all_5m: pd.DataFrame) -> dict:
    entry_ts = _parse_ts(trade["entry_ts"])
    win_start = entry_ts - LOOKBACK
    # Close-trim: only buckets where bucket_end <= entry_ts, i.e. bucket_start + 5min <= entry_ts.
    cutoff_start = entry_ts - BUCKET_DELTA
    df = all_5m[(all_5m.index >= win_start) & (all_5m.index <= cutoff_start)].copy()

    out = dict(trade)
    out["f1_5m_macd_hist"] = None
    out["f2_5m_di_diff"] = None
    out["f3_5m_close_minus_ema20"] = None

    # Need enough rows for MACD (slow=26 + signal=9 warmup) and DMI (period=14).
    # Use 30 as a soft minimum.
    if len(df) < 30:
        return out

    try:
        macd_df = MACD().compute(df, MACD_PARAMS)
        dmi_df = DMI().compute(df, DMI_PARAMS)
        ema_df = MA().compute(df, EMA20_PARAMS)

        hist = macd_df["hist"].iloc[-1]
        plus_di = dmi_df["plus_di"].iloc[-1]
        minus_di = dmi_df["minus_di"].iloc[-1]
        ema20 = ema_df["ma"].iloc[-1]
        last_close = df["close"].iloc[-1]

        if pd.notna(hist):
            out["f1_5m_macd_hist"] = round(float(hist), 4)
        if pd.notna(plus_di) and pd.notna(minus_di):
            out["f2_5m_di_diff"] = round(float(plus_di - minus_di), 4)
        if pd.notna(ema20) and pd.notna(last_close):
            out["f3_5m_close_minus_ema20"] = round(float(last_close - ema20), 4)
    except Exception as exc:  # pragma: no cover — defensive
        out["_enrich_err"] = repr(exc)
    return out


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    # Linear interpolation, matches numpy default
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "median": None, "p10": None, "p25": None, "p75": None, "p90": None}
    return {
        "n": len(values),
        "median": round(statistics.median(values), 4),
        "p10": round(_percentile(values, 0.10), 4),
        "p25": round(_percentile(values, 0.25), 4),
        "p75": round(_percentile(values, 0.75), 4),
        "p90": round(_percentile(values, 0.90), 4),
    }


def _wl_table(enriched: list[dict]) -> dict[str, Any]:
    fields = ["f1_5m_macd_hist", "f2_5m_di_diff", "f3_5m_close_minus_ema20"]
    out: dict[str, Any] = {}
    for f in fields:
        w_vals = [t[f] for t in enriched if t["pnl_points"] > 0 and t[f] is not None]
        l_vals = [t[f] for t in enriched if t["pnl_points"] < 0 and t[f] is not None]
        w_sum = _summarize(w_vals)
        l_sum = _summarize(l_vals)
        sep = None
        if w_sum["median"] is not None and l_sum["median"] is not None:
            sep = round(w_sum["median"] - l_sum["median"], 4)
        out[f] = {"W": w_sum, "L": l_sum, "separation_W_minus_L": sep}
    return out


def _filter_impl_table(enriched: list[dict]) -> dict[str, Any]:
    # "Retained" = signal value > 0 (would pass a "block if <= 0" filter)
    # Trades with None for a given signal are excluded from that signal's bucket counts.
    out: dict[str, Any] = {}
    for f in ["f1_5m_macd_hist", "f2_5m_di_diff", "f3_5m_close_minus_ema20"]:
        retained_pnl: list[float] = []
        retained_wins = 0
        blocked_pnl: list[float] = []
        blocked_wins = 0
        for t in enriched:
            v = t[f]
            if v is None:
                continue
            pnl = t["pnl_points"]
            if v > 0:
                retained_pnl.append(pnl)
                if pnl > 0:
                    retained_wins += 1
            else:
                blocked_pnl.append(pnl)
                if pnl > 0:
                    blocked_wins += 1

        def _bucket(pnls: list[float], wins: int) -> dict[str, Any]:
            n = len(pnls)
            return {
                "n": n,
                "wr_pct": round(100.0 * wins / n, 2) if n else None,
                "sum_pnl": round(sum(pnls), 2) if n else None,
                "mean_pnl": round(sum(pnls) / n, 3) if n else None,
            }

        out[f] = {
            "retained_gt_0": _bucket(retained_pnl, retained_wins),
            "blocked_le_0": _bucket(blocked_pnl, len(blocked_pnl) - sum(1 for p in blocked_pnl if p > 0)),
        }
        # Fix blocked wins count (computed above incorrectly via lambda) — recompute cleanly
        b_wins = sum(1 for p in blocked_pnl if p > 0)
        out[f]["blocked_le_0"] = {
            "n": len(blocked_pnl),
            "wr_pct": round(100.0 * b_wins / len(blocked_pnl), 2) if blocked_pnl else None,
            "sum_pnl": round(sum(blocked_pnl), 2) if blocked_pnl else None,
            "mean_pnl": round(sum(blocked_pnl) / len(blocked_pnl), 3) if blocked_pnl else None,
        }
    return out


def _print_wl(wl: dict[str, Any]) -> None:
    print("\n=== Section 2: W vs L distribution (3 signals) ===")
    header = f"{'signal':<28}{'side':>6}{'n':>6}{'P10':>10}{'P25':>10}{'median':>10}{'P75':>10}{'P90':>10}"
    print(header)
    print("-" * len(header))
    for f, data in wl.items():
        for side in ("W", "L"):
            s = data[side]
            print(
                f"{f:<28}{side:>6}{s['n']:>6}"
                f"{(s['p10'] if s['p10'] is not None else float('nan')):>10.3f}"
                f"{(s['p25'] if s['p25'] is not None else float('nan')):>10.3f}"
                f"{(s['median'] if s['median'] is not None else float('nan')):>10.3f}"
                f"{(s['p75'] if s['p75'] is not None else float('nan')):>10.3f}"
                f"{(s['p90'] if s['p90'] is not None else float('nan')):>10.3f}"
            )
        sep = data["separation_W_minus_L"]
        print(f"{f:<28}{'sep':>6}{'':>6}{'':>10}{'':>10}{(sep if sep is not None else float('nan')):>10.3f}")


def _print_filter_impl(fi: dict[str, Any]) -> None:
    print("\n=== Section 3: Filter-implication (signal > 0 retains) ===")
    header = f"{'signal':<28}{'bucket':<14}{'n':>5}{'wr%':>8}{'sum_pnl':>12}{'mean_pnl':>11}"
    print(header)
    print("-" * len(header))
    for f, data in fi.items():
        r = data["retained_gt_0"]
        b = data["blocked_le_0"]
        for label, bucket in (("retained", r), ("blocked", b)):
            wr = bucket["wr_pct"]
            sp = bucket["sum_pnl"]
            mp = bucket["mean_pnl"]
            print(
                f"{f:<28}{label:<14}{bucket['n']:>5}"
                f"{(wr if wr is not None else float('nan')):>8.2f}"
                f"{(sp if sp is not None else float('nan')):>12.2f}"
                f"{(mp if mp is not None else float('nan')):>11.3f}"
            )


async def main() -> None:
    trades = json.loads(TRADE_DUMP_PATH.read_text())
    print(f"Loaded {len(trades)} trades from {TRADE_DUMP_PATH}")

    await init_engine()
    try:
        await _run(trades)
    finally:
        await dispose_engine()


async def _run(trades: list[dict]) -> None:
    print("Fetching global 5m bar window...")
    all_5m = await _fetch_global_window(trades)
    print(f"Fetched {len(all_5m)} 5m bars covering {all_5m.index.min()} .. {all_5m.index.max()}")

    enriched: list[dict] = []
    n_missing = 0
    for t in trades:
        e = _enrich_trade(t, all_5m)
        if e["f1_5m_macd_hist"] is None:
            n_missing += 1
        enriched.append(e)
    print(f"Enriched: missing={n_missing}/{len(enriched)} (insufficient history before entry_ts)")

    ENRICHED_PATH.write_text(json.dumps(enriched, default=str))
    print(f"Wrote {ENRICHED_PATH}")

    wl = _wl_table(enriched)
    fi = _filter_impl_table(enriched)
    _print_wl(wl)
    _print_filter_impl(fi)

    n_W = sum(1 for t in enriched if t["pnl_points"] > 0)
    n_L = sum(1 for t in enriched if t["pnl_points"] < 0)
    n_zero = sum(1 for t in enriched if t["pnl_points"] == 0)
    print(f"\nTotals: n_W={n_W} n_L={n_L} n_zero={n_zero} (zeros dropped from W/L table)")


if __name__ == "__main__":
    asyncio.run(main())
