"""One-shot cleanup: remove bogus ticks and refresh continuous aggregates.

Removes two classes of bad rows that have polluted continuous-aggregate `low`
values and produced wicks-to-baseline on the chart:

1. Zero or negative prices (legacy bug from the live snapshot adapter, now
   fixed at the boundary in `app.adapters.finmind_taiex`).
2. Calendar-spread quotes from the historical backfill — TaiwanFuturesTick
   mixes outright trades with TAIFEX-listed combos whose `price` field is the
   spread differential (typically -500..+500), not an absolute index level.
   The backfill adapter (`app.ingest.backfill`) now drops these at ingest, but
   any rows already persisted before the fix need to be retroactively removed.
   A floor of 1000 cleanly separates spread quotes from outright TAIEX
   futures (which have not been below 5,000 since the 1980s).

Run: docker compose exec backend uv run python scripts/purge_zero_ticks.py
Or:  cd backend && uv run python scripts/purge_zero_ticks.py
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.db.engine import dispose_engine, get_engine, init_engine, session_scope
from app.ingest.constants import PRICE_FLOOR

log = logging.getLogger("taiex.purge")

# Keep in sync with CONT_AGG_RESOLUTIONS in
# backend/app/db/migrations/versions/0001_init.py — adding a new continuous
# aggregate there must be mirrored here so the cleanup refresh covers it.
CAGG_LABELS = ["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d"]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await init_engine()
    try:
        async with session_scope() as s:
            result = await s.execute(
                text("DELETE FROM ticks WHERE price < :floor"),
                {"floor": PRICE_FLOOR},
            )
            log.info(
                "deleted %d tick row(s) with price < %.1f",
                result.rowcount or 0,
                PRICE_FLOOR,
            )
            await s.commit()

        # Refresh continuous aggregates so chart history reflects cleaned data
        # immediately. CALL must run outside a transaction; use a fresh
        # autocommit connection.
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for label in CAGG_LABELS:
                log.info("refreshing bars_%s ...", label)
                await conn.execute(
                    text("CALL refresh_continuous_aggregate(:name, NULL, NULL)"),
                    {"name": f"bars_{label}"},
                )
        log.info("done; 1w / 1mo views auto-update from bars_1d")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
