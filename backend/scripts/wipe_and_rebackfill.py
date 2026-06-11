"""Nuclear cleanup: TRUNCATE ticks + refresh continuous aggregates.

The backfill adapter has been corrected to filter calendar spreads, sub-floor
prices, and back-month contracts. Existing rows in the `ticks` hypertable
predate that logic and contain mixed contracts that pollute every continuous
aggregate. Selective deletion (e.g. by `price < 1000`) doesn't help because
back-month rows have plausible-looking prices (e.g. 40,550 for Mar 2027).

This script does a hard reset: TRUNCATE the table, then refresh every
continuous aggregate so they materialize an empty state. The next backend
start will repopulate via auto-backfill (`BACKFILL_ON_STARTUP_DAYS`) and the
live snapshot adapter, both now using the front-month-only filters.

Run: docker compose exec backend uv run python scripts/wipe_and_rebackfill.py
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.db.engine import dispose_engine, get_engine, init_engine, session_scope

log = logging.getLogger("taiex.wipe")

CAGG_LABELS = ["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d"]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await init_engine()
    try:
        async with session_scope() as s:
            before = (await s.execute(text("SELECT count(*) FROM ticks"))).scalar() or 0
            log.info("ticks before truncate: %d", before)
            await s.execute(text("TRUNCATE TABLE ticks"))
            await s.commit()
            log.info("ticks truncated")

        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for label in CAGG_LABELS:
                log.info("refreshing bars_%s ...", label)
                await conn.execute(
                    text("CALL refresh_continuous_aggregate(:name, NULL, NULL)"),
                    {"name": f"bars_{label}"},
                )
        log.info("done; restart backend to trigger auto-backfill with new logic")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
