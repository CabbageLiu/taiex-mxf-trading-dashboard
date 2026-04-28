"""initial schema with timescaledb hypertables and continuous aggregates

Revision ID: 0001_init
Revises:
Create Date: 2026-04-28
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_init"
down_revision = None
branch_labels = None
depends_on = None

CONT_AGG_RESOLUTIONS = [
    ("1m", "1 minute"),
    ("5m", "5 minutes"),
    ("15m", "15 minutes"),
    ("30m", "30 minutes"),
    ("1h", "1 hour"),
    ("4h", "4 hours"),
    ("12h", "12 hours"),
    ("1d", "1 day"),
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.execute(
        """
        CREATE TABLE ticks (
            ts        TIMESTAMPTZ NOT NULL,
            symbol    TEXT NOT NULL,
            price     DOUBLE PRECISION NOT NULL,
            source    TEXT NOT NULL,
            PRIMARY KEY (symbol, ts)
        )
        """
    )
    op.execute("SELECT create_hypertable('ticks', 'ts')")
    op.execute("CREATE INDEX ix_ticks_ts ON ticks (ts DESC)")

    op.execute(
        """
        CREATE TABLE signals (
            id         BIGSERIAL PRIMARY KEY,
            ts         TIMESTAMPTZ NOT NULL,
            symbol     TEXT NOT NULL,
            resolution TEXT NOT NULL,
            strategy   TEXT NOT NULL,
            side       TEXT NOT NULL,
            price      DOUBLE PRECISION,
            payload    JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute("CREATE INDEX ix_signals_ts ON signals (ts DESC)")
    op.execute("CREATE INDEX ix_signals_strategy_ts ON signals (strategy, ts DESC)")

    op.execute(
        """
        CREATE TABLE alerts (
            id         BIGSERIAL PRIMARY KEY,
            ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
            signal_id  BIGINT REFERENCES signals(id) ON DELETE SET NULL,
            channel    TEXT NOT NULL,
            status     TEXT NOT NULL,
            http_code  INTEGER,
            error      TEXT
        )
        """
    )

    op.execute(
        """
        CREATE TABLE strategy_config (
            name      TEXT PRIMARY KEY,
            enabled   BOOLEAN NOT NULL DEFAULT FALSE,
            params    JSONB NOT NULL DEFAULT '{}'::jsonb,
            channels  TEXT[] NOT NULL DEFAULT '{discord,n8n,inapp}'
        )
        """
    )

    for label, interval in CONT_AGG_RESOLUTIONS:
        op.execute(
            f"""
            CREATE MATERIALIZED VIEW bars_{label}
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
                schedule_interval => INTERVAL '30 seconds'
            )
            """
        )

    # 1w / 1mo as plain views over 1d (continuous-agg-of-continuous-agg has limits)
    for label, interval in [("1w", "1 week"), ("1mo", "1 month")]:
        op.execute(
            f"""
            CREATE VIEW bars_{label} AS
            SELECT
                symbol,
                time_bucket(INTERVAL '{interval}', bucket) AS bucket,
                first(open, bucket) AS open,
                max(high)           AS high,
                min(low)            AS low,
                last(close, bucket) AS close,
                sum(tick_count)     AS tick_count
            FROM bars_1d
            GROUP BY symbol, time_bucket(INTERVAL '{interval}', bucket)
            """
        )


def downgrade() -> None:
    for label, _ in [("1mo", "1 month"), ("1w", "1 week")]:
        op.execute(f"DROP VIEW IF EXISTS bars_{label}")
    for label, _ in reversed(CONT_AGG_RESOLUTIONS):
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS bars_{label}")
    op.execute("DROP TABLE IF EXISTS strategy_config")
    op.execute("DROP TABLE IF EXISTS alerts")
    op.execute("DROP TABLE IF EXISTS signals")
    op.execute("DROP TABLE IF EXISTS ticks")
