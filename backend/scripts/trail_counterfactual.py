"""Counterfactual: what if TRAIL exit didn't fire?

For every TRAIL-exit trade (entry_ts >= 2026-05-08), replay 1m price path
from entry_ts forward through end-of-session, and measure:

  - MAE (max adverse excursion) in points
  - MFE (max favorable excursion) in points
  - Whether TP target would have been hit
  - PnL at horizon (force-close at session end or +6 hours)
  - Delta vs actual TRAIL exit pnl (positive = TRAIL hurt us, negative = TRAIL saved us)

Horizon: min(entry_ts + 6h, next 13:45 or 05:00 cutoff).

TP target lookup mirrors strat_1k._exit_params_for:
  [08:45, 10:31) → 50
  [10:31, 13:45) → 40
  [15:00, 18:01) → 30
  [18:01, 23:31) → 50
  [23:31, 24:00) ∪ [00:00, 05:00) → 30
  otherwise → 40
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.api.routes.bars import load_bars
from app.db.engine import dispose_engine, init_engine, session_scope

TZ = ZoneInfo("Asia/Taipei")


def _tp_for(ts_utc: datetime) -> float:
    """Mirror strat_1k._exit_params_for TP-only."""
    t = ts_utc.astimezone(TZ).time()
    if time(8, 45) <= t < time(10, 31):
        return 50.0
    if time(10, 31) <= t < time(13, 45):
        return 40.0
    if time(15, 0) <= t < time(18, 1):
        return 30.0
    if time(18, 1) <= t < time(23, 31):
        return 50.0
    if t >= time(23, 31) or t < time(5, 0):
        return 30.0
    return 40.0


def _session_end(entry_utc: datetime) -> datetime:
    """Approximate force-close: 13:45 if entry in day session, 05:00 next day if night."""
    local = entry_utc.astimezone(TZ)
    t = local.time()
    if time(8, 45) <= t < time(13, 45):
        end_local = local.replace(hour=13, minute=45, second=0, microsecond=0)
    elif time(15, 0) <= t < time(23, 59, 59):
        # Night session ends 05:00 next morning (TAIFEX)
        next_day = (local + timedelta(days=1)).date()
        end_local = datetime.combine(next_day, time(5, 0), tzinfo=TZ)
    else:
        # overnight 00:00-05:00
        end_local = local.replace(hour=5, minute=0, second=0, microsecond=0)
        if end_local <= local:
            end_local += timedelta(days=1)
    return end_local.astimezone(timezone.utc)


async def main() -> None:
    await init_engine()
    try:
        await _run()
    finally:
        await dispose_engine()


async def _run() -> None:
    async with session_scope() as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT t.id, t.symbol, t.side, t.entry_ts, t.exit_ts,
                           t.entry_price, t.exit_price, t.pnl_points,
                           sx.payload AS exit_payload
                    FROM trades t
                    LEFT JOIN signals sx ON sx.id = t.exit_signal_id
                    WHERE t.exit_ts IS NOT NULL
                      AND t.entry_ts >= '2026-05-08'::timestamptz
                      AND t.pnl_points < 0
                    ORDER BY t.entry_ts
                    """
                )
            )
        ).mappings().all()

    trail_rows = []
    for r in rows:
        payload = r["exit_payload"] or {}
        reason = payload.get("reason") if isinstance(payload, dict) else None
        if reason and "TRAIL" in reason:
            trail_rows.append(r)

    print(f"TRAIL-exit losing trades: {len(trail_rows)}")
    print()

    saved = 0      # TRAIL saved a bigger loss
    hurt_win = 0   # TRAIL cut a winner (TP would have hit)
    hurt_better = 0  # TRAIL exit worse than holding (final pnl > actual)
    neutral = 0
    deltas = []
    details = []

    for r in trail_rows:
        entry_ts = r["entry_ts"]
        entry_px = float(r["entry_price"])
        side = r["side"]
        actual_pnl = float(r["pnl_points"])
        tp = _tp_for(entry_ts)
        horizon = min(entry_ts + timedelta(hours=6), _session_end(entry_ts))

        bars = await load_bars(r["symbol"], "1m", start=entry_ts, end=horizon)
        if bars.empty:
            continue
        # For LONG: pnl(t) = price(t) - entry; MAE = min, MFE = max.
        # Strat is LONG-only post-05-08 but keep generic.
        if side == "LONG":
            pnl_path = bars["close"] - entry_px
            mae_series = bars["low"] - entry_px
            mfe_series = bars["high"] - entry_px
        else:
            pnl_path = entry_px - bars["close"]
            mae_series = entry_px - bars["high"]
            mfe_series = entry_px - bars["low"]

        mae = float(mae_series.min())
        mfe = float(mfe_series.max())
        final_pnl = float(pnl_path.iloc[-1])
        tp_hit = mfe >= tp

        # If continued holding, exit would have been TP (if reached) else final.
        cf_pnl = tp if tp_hit else final_pnl
        delta = cf_pnl - actual_pnl   # positive = held would have been better
        deltas.append(delta)

        if tp_hit:
            hurt_win += 1
        elif cf_pnl > actual_pnl + 5:
            hurt_better += 1
        elif cf_pnl < actual_pnl - 5:
            saved += 1
        else:
            neutral += 1

        details.append({
            "id": r["id"],
            "side": side,
            "actual_pnl": actual_pnl,
            "tp_target": tp,
            "mae": mae,
            "mfe": mfe,
            "final_pnl": final_pnl,
            "tp_hit": tp_hit,
            "cf_pnl": cf_pnl,
            "delta": delta,
        })

    n = len(details)
    if n == 0:
        print("no data")
        return

    print(f"{'verdict':<28}{'n':>5}  {'%':>5}")
    print(f"{'TRAIL cut a winner (TP hit)':<28}{hurt_win:>5}  {100*hurt_win/n:>4.1f}%")
    print(f"{'TRAIL exit worse than hold':<28}{hurt_better:>5}  {100*hurt_better/n:>4.1f}%")
    print(f"{'TRAIL saved bigger loss':<28}{saved:>5}  {100*saved/n:>4.1f}%")
    print(f"{'neutral (±5 pt)':<28}{neutral:>5}  {100*neutral/n:>4.1f}%")
    print()
    print(f"avg delta (hold - TRAIL): {sum(deltas)/len(deltas):+.1f} pts")
    print(f"sum delta (hold - TRAIL): {sum(deltas):+.1f} pts across {n} trades")
    print()
    print("=== detail (most-hurt-by-TRAIL first) ===")
    print(f"{'id':>5}{'side':>6}{'actual':>9}{'tp':>6}{'mae':>9}{'mfe':>9}{'final':>9}{'tp_hit':>8}{'cf':>8}{'delta':>9}")
    for d in sorted(details, key=lambda x: -x["delta"])[:25]:
        print(f"{d['id']:>5}{d['side']:>6}{d['actual_pnl']:>+9.1f}{d['tp_target']:>+6.0f}"
              f"{d['mae']:>+9.1f}{d['mfe']:>+9.1f}{d['final_pnl']:>+9.1f}"
              f"{'Y' if d['tp_hit'] else 'N':>8}{d['cf_pnl']:>+8.1f}{d['delta']:>+9.1f}")
    print()
    print("=== detail (most-saved-by-TRAIL first) ===")
    for d in sorted(details, key=lambda x: x["delta"])[:10]:
        print(f"{d['id']:>5}{d['side']:>6}{d['actual_pnl']:>+9.1f}{d['tp_target']:>+6.0f}"
              f"{d['mae']:>+9.1f}{d['mfe']:>+9.1f}{d['final_pnl']:>+9.1f}"
              f"{'Y' if d['tp_hit'] else 'N':>8}{d['cf_pnl']:>+8.1f}{d['delta']:>+9.1f}")


if __name__ == "__main__":
    asyncio.run(main())
