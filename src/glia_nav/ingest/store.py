"""Writes ingested records to Aurora through the RDS Data API, and raw payloads to S3.

The Data API takes no array parameters, so lists travel as JSON strings and are unpacked
in SQL. BatchExecuteStatement caps the request size, so rows go in batches of BATCH_ROWS.
"""

import json
import time
from datetime import date
from itertools import batched

from glia_nav.config import IngestSettings

BATCH_ROWS = 50

TEXT_ARRAY = "ARRAY(SELECT jsonb_array_elements_text(CAST(:{} AS jsonb)))"

UPSERT_TRIAL = f"""
INSERT INTO trials (
    nct_id, title, brief_summary, overall_status, phases, study_type, conditions,
    interventions, eligibility_criteria, min_age_years, max_age_years, sex,
    healthy_volunteers, sponsor, start_date, primary_completion_date, last_update_posted,
    raw_s3_key, search
) VALUES (
    :nct_id, :title, :brief_summary, :overall_status, {TEXT_ARRAY.format("phases")},
    :study_type, {TEXT_ARRAY.format("conditions")}, CAST(:interventions AS jsonb),
    :eligibility_criteria, :min_age_years, :max_age_years, :sex, :healthy_volunteers,
    :sponsor, :start_date, :primary_completion_date, :last_update_posted, :raw_s3_key,
    to_tsvector('english', :search_text)
)
ON CONFLICT (nct_id) DO UPDATE SET
    title = EXCLUDED.title,
    brief_summary = EXCLUDED.brief_summary,
    overall_status = EXCLUDED.overall_status,
    phases = EXCLUDED.phases,
    study_type = EXCLUDED.study_type,
    conditions = EXCLUDED.conditions,
    interventions = EXCLUDED.interventions,
    eligibility_criteria = EXCLUDED.eligibility_criteria,
    min_age_years = EXCLUDED.min_age_years,
    max_age_years = EXCLUDED.max_age_years,
    sex = EXCLUDED.sex,
    healthy_volunteers = EXCLUDED.healthy_volunteers,
    sponsor = EXCLUDED.sponsor,
    start_date = EXCLUDED.start_date,
    primary_completion_date = EXCLUDED.primary_completion_date,
    last_update_posted = EXCLUDED.last_update_posted,
    raw_s3_key = EXCLUDED.raw_s3_key,
    search = EXCLUDED.search,
    ingested_at = now()
"""

INSERT_SITE = """
INSERT INTO trial_sites (nct_id, facility, city, state, country, status, latitude, longitude)
VALUES (:nct_id, :facility, :city, :state, :country, :status, :latitude, :longitude)
"""

UPSERT_PAPER = f"""
INSERT INTO papers (
    pmid, title, abstract, journal, pub_date, pub_types, mesh_terms, doi, pmcid,
    last_revised, raw_s3_key, search
) VALUES (
    :pmid, :title, :abstract, :journal, :pub_date, {TEXT_ARRAY.format("pub_types")},
    {TEXT_ARRAY.format("mesh_terms")}, :doi, :pmcid, :last_revised, :raw_s3_key,
    to_tsvector('english', :search_text)
)
ON CONFLICT (pmid) DO UPDATE SET
    title = EXCLUDED.title,
    abstract = EXCLUDED.abstract,
    journal = EXCLUDED.journal,
    pub_date = EXCLUDED.pub_date,
    pub_types = EXCLUDED.pub_types,
    mesh_terms = EXCLUDED.mesh_terms,
    doi = EXCLUDED.doi,
    pmcid = EXCLUDED.pmcid,
    last_revised = EXCLUDED.last_revised,
    raw_s3_key = EXCLUDED.raw_s3_key,
    search = EXCLUDED.search,
    ingested_at = now()
"""

INSERT_LINK = """
INSERT INTO trial_papers (nct_id, pmid, source) VALUES (:nct_id, :pmid, :source)
ON CONFLICT DO NOTHING
"""

UPSERT_CHUNK = """
INSERT INTO chunks (source, source_id, section, text, embedding, model, content_hash)
VALUES (:source, :source_id, :section, :text, CAST(:embedding AS vector), :model, :content_hash)
ON CONFLICT (source, source_id, section) DO UPDATE SET
    text = EXCLUDED.text,
    embedding = EXCLUDED.embedding,
    model = EXCLUDED.model,
    content_hash = EXCLUDED.content_hash
"""


UPSERT_ELIGIBILITY = """
INSERT INTO trial_eligibility (
    nct_id, setting, max_recurrence, idh, mgmt, prior_bevacizumab, min_kps, model,
    criteria_hash
) VALUES (
    :nct_id, :setting, :max_recurrence, :idh, :mgmt, :prior_bevacizumab, :min_kps, :model,
    :criteria_hash
)
ON CONFLICT (nct_id) DO UPDATE SET
    setting = EXCLUDED.setting,
    max_recurrence = EXCLUDED.max_recurrence,
    idh = EXCLUDED.idh,
    mgmt = EXCLUDED.mgmt,
    prior_bevacizumab = EXCLUDED.prior_bevacizumab,
    min_kps = EXCLUDED.min_kps,
    model = EXCLUDED.model,
    criteria_hash = EXCLUDED.criteria_hash,
    extracted_at = now()
"""


def param(name: str, value) -> dict:
    """One Data API parameter. Lists and dicts go as JSON; the SQL casts them back."""
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    if isinstance(value, bool):
        return {"name": name, "value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"name": name, "value": {"longValue": value}}
    if isinstance(value, float):
        return {"name": name, "value": {"doubleValue": value}}
    if isinstance(value, date):
        return {"name": name, "typeHint": "DATE", "value": {"stringValue": value.isoformat()}}
    if isinstance(value, list | dict):
        return {"name": name, "value": {"stringValue": json.dumps(value)}}
    return {"name": name, "value": {"stringValue": value}}


def trial_raw_key(nct_id: str) -> str:
    return f"raw/ctgov/{nct_id}.json"


def paper_raw_key(pmid: str) -> str:
    return f"raw/pubmed/{pmid}.xml"


def params(row: dict) -> list[dict]:
    return [param(k, v) for k, v in row.items()]


class Store:
    def __init__(self, settings: IngestSettings, rds, s3):
        self.settings = settings
        self.rds = rds
        self.s3 = s3
        self.target = {
            "resourceArn": settings.database_cluster_arn,
            "secretArn": settings.database_secret_arn,
            "database": settings.database_name,
        }

    def wake(self, attempts: int = 12, delay: float = 10) -> None:
        """Block until the cluster answers. It auto-pauses after 5 idle minutes and takes
        up to a minute to resume, failing every call until then."""
        for attempt in range(attempts):
            try:
                self.execute("SELECT 1")
                return
            except self.rds.exceptions.DatabaseResumingException:
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)

    def execute(self, sql: str, row: dict | None = None) -> list:
        response = self.rds.execute_statement(
            **self.target, sql=sql, parameters=params(row or {}), formatRecordsAs="JSON"
        )
        return json.loads(response.get("formattedRecords", "[]"))

    def execute_batch(self, sql: str, rows: list[dict]) -> None:
        for i in range(0, len(rows), BATCH_ROWS):
            self.rds.batch_execute_statement(
                **self.target,
                sql=sql,
                parameterSets=[params(r) for r in rows[i : i + BATCH_ROWS]],
            )

    def put_raw(self, key: str, body: bytes, content_type: str) -> None:
        self.s3.put_object(
            Bucket=self.settings.documents_bucket, Key=key, Body=body, ContentType=content_type
        )

    def upsert_trials(self, trials: list) -> None:
        self.execute_batch(
            UPSERT_TRIAL,
            [
                t.row | {"raw_s3_key": trial_raw_key(t.row["nct_id"]), "search_text": t.search_text}
                for t in trials
            ],
        )
        # Sites have no stable id upstream, so a trial's sites are replaced wholesale.
        self.execute(
            "DELETE FROM trial_sites WHERE nct_id IN "
            "(SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
            {"ids": [t.row["nct_id"] for t in trials]},
        )
        self.execute_batch(
            INSERT_SITE, [s | {"nct_id": t.row["nct_id"]} for t in trials for s in t.sites]
        )
        self.execute_batch(
            INSERT_LINK,
            [
                {"nct_id": t.row["nct_id"], "pmid": pmid, "source": "ctgov"}
                for t in trials
                for pmid in t.pmids
            ],
        )

    def upsert_papers(self, papers: list) -> None:
        self.execute_batch(
            UPSERT_PAPER,
            [
                p.row | {"raw_s3_key": paper_raw_key(p.row["pmid"]), "search_text": p.search_text}
                for p in papers
            ],
        )
        self.execute_batch(
            INSERT_LINK,
            [
                {"nct_id": nct_id, "pmid": p.row["pmid"], "source": "pubmed"}
                for p in papers
                for nct_id in p.nct_ids
            ],
        )

    def paper_ids(self, page: int = 20000) -> set[str]:
        """Every stored PMID. Paged: the Data API refuses a result over 1 MB."""
        pmids: set[str] = set()
        after = ""
        while True:
            records = self.execute(
                "SELECT pmid FROM papers WHERE pmid > :after ORDER BY pmid LIMIT :page",
                {"after": after, "page": page},
            )
            pmids.update(r["pmid"] for r in records)
            if len(records) < page:
                return pmids
            after = records[-1]["pmid"]

    def delete_papers(self, pmids: list[str]) -> None:
        """Remove papers with their chunks, PubMed-sourced trial links, and raw XML.
        Links CT.gov asserted stay: the trial record still cites the PMID."""
        ids = "(SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))"
        for pmid_batch in batched(pmids, 1000, strict=False):
            batch = list(pmid_batch)
            self.execute(
                f"DELETE FROM chunks WHERE source = 'paper' AND source_id IN {ids}", {"ids": batch}
            )
            self.execute(
                f"DELETE FROM trial_papers WHERE source = 'pubmed' AND pmid IN {ids}",
                {"ids": batch},
            )
            self.execute(f"DELETE FROM papers WHERE pmid IN {ids}", {"ids": batch})
            self.s3.delete_objects(
                Bucket=self.settings.documents_bucket,
                Delete={"Objects": [{"Key": paper_raw_key(p)} for p in batch], "Quiet": True},
            )

    def chunk_hashes(self, source: str, source_ids: list[str]) -> dict[tuple[str, str], str]:
        records = self.execute(
            "SELECT source_id, section, content_hash FROM chunks WHERE source = :source "
            "AND source_id IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
            {"source": source, "ids": source_ids},
        )
        return {(r["source_id"], r["section"]): r["content_hash"] for r in records}

    def upsert_chunks(self, rows: list[dict]) -> None:
        self.execute_batch(UPSERT_CHUNK, rows)

    def eligibility_hashes(self, nct_ids: list[str]) -> dict[str, str]:
        records = self.execute(
            "SELECT nct_id, criteria_hash FROM trial_eligibility WHERE nct_id IN "
            "(SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
            {"ids": nct_ids},
        )
        return {r["nct_id"]: r["criteria_hash"] for r in records}

    def upsert_eligibility(self, rows: list[dict]) -> None:
        self.execute_batch(UPSERT_ELIGIBILITY, rows)

    def delete_eligibility(self, nct_ids: list[str]) -> None:
        self.execute(
            "DELETE FROM trial_eligibility WHERE nct_id IN "
            "(SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
            {"ids": nct_ids},
        )

    def start_run(self, source: str) -> int:
        records = self.execute(
            "INSERT INTO ingest_runs (source, status) VALUES (:source, 'running') RETURNING id",
            {"source": source},
        )
        return records[0]["id"]

    def finish_run(self, run_id: int, status: str, watermark: date | None, **counts) -> None:
        self.execute(
            "UPDATE ingest_runs SET status = :status, watermark = :watermark, "
            "finished_at = now(), fetched = :fetched, upserted = :upserted, "
            "embedded = :embedded, error = :error WHERE id = :id",
            {
                "id": run_id,
                "status": status,
                "watermark": watermark,
                "fetched": counts.get("fetched", 0),
                "upserted": counts.get("upserted", 0),
                "embedded": counts.get("embedded", 0),
                "error": counts.get("error"),
            },
        )

    def last_watermark(self, source: str) -> date | None:
        records = self.execute(
            "SELECT max(watermark)::text AS watermark FROM ingest_runs "
            "WHERE source = :source AND status = 'succeeded'",
            {"source": source},
        )
        value = records[0]["watermark"] if records else None
        return date.fromisoformat(value) if value else None
