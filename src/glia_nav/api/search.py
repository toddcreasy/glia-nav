"""Hybrid search over trials and papers: full-text and vector results fused by rank.

Keyword search finds exact identifiers, drug names, and genes that embeddings blur.
Vector search finds matches that share meaning but not words. Reciprocal rank fusion
merges the two lists without having to calibrate their scores against each other.
"""

import json

from glia_nav.config import Settings
from glia_nav.ingest.embed import embed
from glia_nav.ingest.store import param

# Candidates taken from each list before fusion, and the standard RRF damping constant.
POOL = 50
RRF_K = 60
# RRF gives the top keyword hit and the top vector hit the same score, so a query naming
# an NCT ID or PMID could rank that record second. A record named outright goes first.
EXACT_BOOST = 1.0

RECRUITING = ("RECRUITING", "NOT_YET_RECRUITING")

# The top keyword hit and the top vector hit score exactly the same. On a tie the keyword
# hit goes first: a query that is a trial's nickname (EF-14) should return that trial.
FUSE = """
fused AS (
    SELECT id, sum(1.0 / (:rrf_k + rank)) AS score, bool_or(kind = 'keyword') AS keyword_hit
    FROM (
        SELECT id, rank, 'semantic' AS kind FROM semantic
        UNION ALL
        SELECT id, rank, 'keyword' AS kind FROM keyword
    ) hits
    GROUP BY id
)
"""

# A site matches every place filter given. Any one of them can be used alone: "trials
# in Boston" should not need the state and country spelled out.
SITE_MATCH = """
    (CAST(:country AS text) IS NULL OR lower(s.country) = lower(CAST(:country AS text)))
    AND (CAST(:state AS text) IS NULL OR lower(s.state) = lower(CAST(:state AS text)))
    AND (CAST(:city AS text) IS NULL OR lower(s.city) = lower(CAST(:city AS text)))
"""

# Patient attributes against a trial's extracted requirements. A NULL requirement, or a
# trial with no extraction yet, matches: a model misreading criteria must not hide a trial.
# A patient at a numbered recurrence also rules out trials for newly diagnosed disease.
ELIGIBILITY_MATCH = """
    AND (CAST(:idh AS text) IS NULL OR el.idh IS NULL OR el.idh = CAST(:idh AS text))
    AND (CAST(:mgmt AS text) IS NULL OR el.mgmt IS NULL OR el.mgmt = CAST(:mgmt AS text))
    AND (CAST(:setting AS text) IS NULL OR el.setting IS NULL
         OR el.setting = CAST(:setting AS text))
    AND (CAST(:recurrence AS integer) IS NULL OR (
            el.setting IS DISTINCT FROM 'newly_diagnosed'
        AND (el.max_recurrence IS NULL OR el.max_recurrence >= CAST(:recurrence AS integer))))
    AND (CAST(:prior_bevacizumab AS boolean) IS NULL OR el.prior_bevacizumab IS NULL
         OR el.prior_bevacizumab = CASE WHEN CAST(:prior_bevacizumab AS boolean)
                                        THEN 'required' ELSE 'excluded' END)
    AND (CAST(:kps AS integer) IS NULL OR el.min_kps IS NULL
         OR el.min_kps <= CAST(:kps AS integer))
"""

TRIALS_SQL = f"""
WITH eligible AS (
    SELECT t.nct_id FROM trials t
    LEFT JOIN trial_eligibility el ON el.nct_id = t.nct_id
    WHERE (NOT :recruiting OR t.overall_status IN {RECRUITING})
      AND (CAST(:phase AS text) IS NULL OR CAST(:phase AS text) = ANY (t.phases))
      AND (CAST(:age AS numeric) IS NULL OR (
            (t.min_age_years IS NULL OR t.min_age_years <= CAST(:age AS numeric))
        AND (t.max_age_years IS NULL OR t.max_age_years >= CAST(:age AS numeric))))
      AND ((CAST(:country AS text) IS NULL AND CAST(:state AS text) IS NULL
            AND CAST(:city AS text) IS NULL)
        OR EXISTS (SELECT 1 FROM trial_sites s WHERE s.nct_id = t.nct_id AND {SITE_MATCH}))
      {ELIGIBILITY_MATCH}
),
semantic AS (
    SELECT c.source_id AS id,
           row_number() OVER (ORDER BY min(c.embedding <=> CAST(:vector AS vector))) AS rank
    FROM chunks c JOIN eligible e ON e.nct_id = c.source_id
    WHERE c.source = 'trial'
    GROUP BY c.source_id
    ORDER BY rank
    LIMIT :pool
),
keyword AS (
    SELECT t.nct_id AS id,
           row_number() OVER (
               ORDER BY ts_rank_cd(t.search, websearch_to_tsquery('english', :q)) DESC
           ) AS rank
    FROM trials t JOIN eligible e USING (nct_id)
    WHERE t.search @@ websearch_to_tsquery('english', :q)
    ORDER BY rank
    LIMIT :pool
),
{FUSE}
SELECT t.nct_id, t.title, t.overall_status, t.phases, t.conditions, t.sponsor,
       t.min_age_years::float8 AS min_age_years, t.max_age_years::float8 AS max_age_years,
       t.last_update_posted::text AS last_update_posted,
       (f.score + CASE WHEN :q ~* ('\\m' || t.nct_id || '\\M') THEN :boost ELSE 0 END)::float8
           AS score,
       (SELECT count(*) FROM trial_sites s WHERE s.nct_id = t.nct_id) AS site_count,
       (SELECT coalesce(jsonb_agg(site), '[]'::jsonb) FROM (
            SELECT jsonb_build_object(
                       'facility', s.facility, 'city', s.city, 'state', s.state,
                       'country', s.country, 'status', s.status) AS site
            FROM trial_sites s
            WHERE s.nct_id = t.nct_id AND {SITE_MATCH}
            LIMIT 5) matching) AS sites,
       (SELECT to_jsonb(e) - 'nct_id' - 'model' - 'criteria_hash' - 'extracted_at'
        FROM trial_eligibility e WHERE e.nct_id = t.nct_id) AS eligibility
FROM fused f JOIN trials t ON t.nct_id = f.id
ORDER BY score DESC, f.keyword_hit DESC
LIMIT :limit
"""

PAPERS_SQL = f"""
WITH eligible AS (
    SELECT p.pmid FROM papers p
    WHERE CAST(:from_year AS integer) IS NULL
       OR p.pub_date >= make_date(CAST(:from_year AS integer), 1, 1)
),
semantic AS (
    SELECT c.source_id AS id,
           row_number() OVER (ORDER BY c.embedding <=> CAST(:vector AS vector)) AS rank
    FROM chunks c JOIN eligible e ON e.pmid = c.source_id
    WHERE c.source = 'paper'
    ORDER BY rank
    LIMIT :pool
),
keyword AS (
    SELECT p.pmid AS id,
           row_number() OVER (
               ORDER BY ts_rank_cd(p.search, websearch_to_tsquery('english', :q)) DESC
           ) AS rank
    FROM papers p JOIN eligible e USING (pmid)
    WHERE p.search @@ websearch_to_tsquery('english', :q)
    ORDER BY rank
    LIMIT :pool
),
{FUSE}
SELECT p.pmid, p.title, p.journal, p.pub_date::text AS pub_date, p.pub_types, p.doi,
       left(p.abstract, 400) AS snippet,
       (f.score + CASE WHEN :q ~ ('\\m' || p.pmid || '\\M') THEN :boost ELSE 0 END)::float8
           AS score,
       coalesce((SELECT array_agg(DISTINCT tp.nct_id ORDER BY tp.nct_id)
                 FROM trial_papers tp WHERE tp.pmid = p.pmid), '{{}}') AS nct_ids
FROM fused f JOIN papers p ON p.pmid = f.id
ORDER BY score DESC, f.keyword_hit DESC
LIMIT :limit
"""


def run(rds, settings: Settings, sql: str, values: dict) -> list[dict]:
    response = rds.execute_statement(
        resourceArn=settings.database_cluster_arn,
        secretArn=settings.database_secret_arn,
        database=settings.database_name,
        sql=sql,
        parameters=[param(k, v) for k, v in values.items()],
        formatRecordsAs="JSON",
    )
    return json.loads(response.get("formattedRecords", "[]"))


def query_vector(bedrock, settings: Settings, q: str) -> str:
    return json.dumps(embed(bedrock, settings.embedding_model, q))


def search_trials(rds, settings: Settings, q: str, vector: str, limit: int, **filters) -> list:
    rows = run(
        rds,
        settings,
        TRIALS_SQL,
        {
            "q": q,
            "vector": vector,
            "pool": POOL,
            "rrf_k": RRF_K,
            "boost": EXACT_BOOST,
            "limit": limit,
        }
        | filters,
    )
    for row in rows:
        # The Data API returns jsonb as its text form.
        for key in ("sites", "eligibility"):
            if isinstance(row[key], str):
                row[key] = json.loads(row[key])
    return rows


def search_papers(
    rds, settings: Settings, q: str, vector: str, limit: int, from_year: int | None
) -> list:
    return run(
        rds,
        settings,
        PAPERS_SQL,
        {
            "q": q,
            "vector": vector,
            "pool": POOL,
            "rrf_k": RRF_K,
            "boost": EXACT_BOOST,
            "limit": limit,
            "from_year": from_year,
        },
    )
