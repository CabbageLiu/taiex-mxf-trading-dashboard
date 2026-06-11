"""trends hypertable for 15m trend snapshots

Revision ID: 0005_trends
Revises: 0004_bars_3m
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_trends"
down_revision = "0004_bars_3m"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trends",
        sa.Column("symbol", sa.String(), primary_key=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True, nullable=False),
        sa.Column("resolution", sa.String(), nullable=False),
        sa.Column("ema20", sa.Float(), nullable=False),
        sa.Column("ema50", sa.Float(), nullable=False),
        sa.Column("plus_di", sa.Float(), nullable=False),
        sa.Column("minus_di", sa.Float(), nullable=False),
        sa.Column("adx", sa.Float(), nullable=False),
        sa.Column("direction", sa.SmallInteger(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
    )
    # Hypertable on ts. ``if_not_exists`` defends against re-runs in dev.
    op.execute(
        "SELECT create_hypertable('trends', 'ts', if_not_exists => TRUE)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trends_symbol_ts_desc "
        "ON trends (symbol, ts DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_trends_symbol_ts_desc")
    op.drop_table("trends")
