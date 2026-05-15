"""Shared ingest-layer constants."""

from __future__ import annotations

# Floor for outright TAIEX-futures tick prices.
#
# Two distinct pollution paths produced sub-floor values in the past:
#
# 1. Live `taiwan_futures_snapshot` occasionally emits `close: 0` between
#    trades. Now rejected at the adapter boundary in
#    `app.adapters.finmind_taiex._rows_to_ticks`.
#
# 2. Historical `TaiwanFuturesTick` mixes outright single-leg trades with
#    TAIFEX-listed calendar-spread combos whose `price` is the spread
#    differential (typically -500..+500), not an absolute index level. The
#    backfill primarily filters spreads by `'/' in contract_date`; the price
#    floor is defense-in-depth for any combo notation we haven't catalogued.
#
# 1000 is conservative — TAIEX hasn't been below 5,000 since the 1980s and
# MTX/TXF futures have always tracked the index. If the project ever ingests
# low-priced instruments (options, weekly micros), this floor must be
# revisited per data_id rather than raised globally.
PRICE_FLOOR: float = 1000.0
