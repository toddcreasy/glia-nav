"""daily chat token usage, for the public demo's daily limit

Revision ID: 0004
Revises: 0003

One row per day, in US Eastern time so the limit resets at local midnight. /chat reads
today's total before each turn and adds the turn's tokens after it.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE daily_usage (
        day date PRIMARY KEY,
        tokens bigint NOT NULL DEFAULT 0,
        turns integer NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
]

DOWNGRADE = ["DROP TABLE daily_usage"]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
