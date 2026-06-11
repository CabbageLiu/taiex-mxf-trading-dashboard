"""trades table for closed-round-trip attribution

Revision ID: 0002_trades
Revises: 0001_init
Create Date: 2026-04-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_trades"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("strategy", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("entry_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column(
            "entry_signal_id",
            sa.BigInteger(),
            sa.ForeignKey("signals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("exit_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column(
            "exit_signal_id",
            sa.BigInteger(),
            sa.ForeignKey("signals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("qty", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("pnl_points", sa.Float(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_trades_strategy", "trades", ["strategy"])
    op.create_index("ix_trades_symbol", "trades", ["symbol"])
    op.create_index("ix_trades_entry_ts", "trades", ["entry_ts"])
    op.create_index("ix_trades_exit_ts", "trades", ["exit_ts"])
    op.create_index(
        "ix_trades_strategy_entry_ts",
        "trades",
        ["strategy", sa.text("entry_ts DESC")],
    )
    op.create_index(
        "ix_trades_symbol_entry_ts",
        "trades",
        ["symbol", sa.text("entry_ts DESC")],
    )
    # Partial unique index: at most one OPEN trade per (strategy, symbol).
    # Defends against double-open races between the strategy loop and tracker
    # restart paths. Closed rows (exit_ts NOT NULL) are unconstrained.
    op.create_index(
        "ux_trades_open_position",
        "trades",
        ["strategy", "symbol"],
        unique=True,
        postgresql_where=sa.text("exit_ts IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_trades_open_position", table_name="trades")
    op.drop_index("ix_trades_symbol_entry_ts", table_name="trades")
    op.drop_index("ix_trades_strategy_entry_ts", table_name="trades")
    op.drop_index("ix_trades_exit_ts", table_name="trades")
    op.drop_index("ix_trades_entry_ts", table_name="trades")
    op.drop_index("ix_trades_symbol", table_name="trades")
    op.drop_index("ix_trades_strategy", table_name="trades")
    op.drop_table("trades")
