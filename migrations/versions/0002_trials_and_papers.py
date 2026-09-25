"""trials, papers, and their embeddings

Revision ID: 0002
Revises: 0001

Raw SQL rather than op.create_table: the Data API dialect has no vector type, and the
Data API runs one statement per call, so each statement is its own op.execute.

The HNSW index on chunks.embedding is left out on purpose. It builds faster once, after
the backfill, than it maintains across 60k single inserts at 1 ACU; it lands in 0003.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE trials (
        nct_id text PRIMARY KEY,
        title text NOT NULL,
        brief_summary text,
        overall_status text NOT NULL,
        phases text[] NOT NULL DEFAULT '{}',
        study_type text,
        conditions text[] NOT NULL DEFAULT '{}',
        interventions jsonb NOT NULL DEFAULT '[]',
        eligibility_criteria text,
        min_age_years numeric,
        max_age_years numeric,
        sex text,
        healthy_volunteers boolean,
        sponsor text,
        start_date date,
        primary_completion_date date,
        last_update_posted date NOT NULL,
        raw_s3_key text NOT NULL,
        search tsvector NOT NULL,
        ingested_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX trials_search_idx ON trials USING gin (search)",
    """
    CREATE TABLE trial_sites (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        nct_id text NOT NULL REFERENCES trials (nct_id) ON DELETE CASCADE,
        facility text,
        city text,
        state text,
        country text,
        status text,
        latitude double precision,
        longitude double precision
    )
    """,
    "CREATE INDEX trial_sites_nct_id_idx ON trial_sites (nct_id)",
    """
    CREATE TABLE papers (
        pmid text PRIMARY KEY,
        title text NOT NULL,
        abstract text,
        journal text,
        pub_date date,
        pub_types text[] NOT NULL DEFAULT '{}',
        mesh_terms text[] NOT NULL DEFAULT '{}',
        doi text,
        pmcid text,
        last_revised date,
        raw_s3_key text NOT NULL,
        search tsvector NOT NULL,
        ingested_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX papers_search_idx ON papers USING gin (search)",
    "CREATE INDEX papers_pub_date_idx ON papers (pub_date)",
    """
    CREATE TABLE trial_papers (
        nct_id text NOT NULL,
        pmid text NOT NULL,
        source text NOT NULL CHECK (source IN ('ctgov', 'pubmed')),
        PRIMARY KEY (nct_id, pmid, source)
    )
    """,
    "CREATE INDEX trial_papers_pmid_idx ON trial_papers (pmid)",
    """
    CREATE TABLE chunks (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        source text NOT NULL CHECK (source IN ('trial', 'paper')),
        source_id text NOT NULL,
        section text NOT NULL,
        text text NOT NULL,
        embedding vector(512) NOT NULL,
        model text NOT NULL,
        content_hash text NOT NULL,
        UNIQUE (source, source_id, section)
    )
    """,
    """
    CREATE TABLE ingest_runs (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        source text NOT NULL CHECK (source IN ('ctgov', 'pubmed')),
        status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
        watermark date,
        started_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz,
        fetched integer NOT NULL DEFAULT 0,
        upserted integer NOT NULL DEFAULT 0,
        embedded integer NOT NULL DEFAULT 0,
        error text
    )
    """,
]

DOWNGRADE = [
    "DROP TABLE ingest_runs",
    "DROP TABLE chunks",
    "DROP TABLE trial_papers",
    "DROP TABLE papers",
    "DROP TABLE trial_sites",
    "DROP TABLE trials",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
