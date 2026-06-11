"""add bars_2m and bars_10m continuous aggregates

Revision ID: 0003_bars_2m_10m
Revises: 0002_trades
Create Date: 2026-04-30
"""

from __future__ import annotations

from alembic import op

revision: str = "0003_bars_2m_10m"
down_revision = "0002_trades"
branch_labels = None
depends_on = None

NEW_RESOLUTIONS = [
    ("2m", "2 minutes"),
    ("10m", "10 minutes"),
]


def upgrade() -> None:
    for label, interval in NEW_RESOLUTIONS:
        op.execute(
            f"""
            CREATE MATERIALIZED VIEW IF NOT EXISTS bars_{label}
            WITH (timescaledb.continuous) AS
            SELECT
                symbol,
                time_bucket(INTERVAL '{interval}', ts) AS bucket,
                first(price, ts) AS open,
                max(price)       AS high,
                min(price)       AS low,
                last(price, ts)  AS close,
                count(*)         AS tick_count
            FROM ticks
            GROUP BY symbol, bucket
            WITH NO DATA
            """
        )
        op.execute(
            f"""
            SELECT add_continuous_aggregate_policy(
                'bars_{label}',
                start_offset => INTERVAL '30 days',
                end_offset   => INTERVAL '{interval}',
                schedule_interval => INTERVAL '30 seconds',
                if_not_exists => TRUE
            )
            """
        )


def downgrade() -> None:
    for label, _ in reversed(NEW_RESOLUTIONS):
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS bars_{label}")
