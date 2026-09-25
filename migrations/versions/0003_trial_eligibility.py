"""structured eligibility, extracted from each trial's criteria text

Revision ID: 0003
Revises: 0002

A table of its own rather than columns on trials: the trial upsert rewrites every trials
column, and extraction runs after it with its own change check on criteria_hash.

Every requirement column is NULL when the criteria place no restriction on it, or when
the model could not tell. Search treats NULL as a match.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE trial_eligibility (
        nct_id text PRIMARY KEY REFERENCES trials (nct_id) ON DELETE CASCADE,
        setting text CHECK (setting IN ('newly_diagnosed', 'recurrent')),
        max_recurrence integer CHECK (max_recurrence >= 1),
        idh text CHECK (idh IN ('wildtype', 'mutant')),
        mgmt text CHECK (mgmt IN ('methylated', 'unmethylated')),
        prior_bevacizumab text CHECK (prior_bevacizumab IN ('excluded', 'required')),
        min_kps integer CHECK (min_kps BETWEEN 0 AND 100),
        model text NOT NULL,
        criteria_hash text NOT NULL,
        extracted_at timestamptz NOT NULL DEFAULT now()
    )
    """,
]

DOWNGRADE = ["DROP TABLE trial_eligibility"]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
