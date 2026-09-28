"""Ingestion runs: fetch, parse, store, embed, and extract trial eligibility. Used by the
backfill script and the scheduled job.

Every write is an upsert, so any run can be repeated safely. Chunks and criteria whose text
hash is unchanged are not re-embedded or re-extracted, which keeps a repeated run close to free.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from itertools import batched

import httpx

from glia_nav.ingest import ctgov, eligibility, pubmed
from glia_nav.ingest.embed import content_hash, embed
from glia_nav.ingest.store import Store, paper_raw_key, trial_raw_key

logger = logging.getLogger("glia_nav.ingest")

# Titan and S3 calls are network-bound; a few threads cut wall time without tripping quotas.
WORKERS = 4
TRIAL_BATCH = 100
# A reconcile that would delete more than this share of stored papers stops instead: an
# ESearch slice that came back short would otherwise read as mass withdrawal.
MAX_REMOVED_SHARE = 0.01
# Re-read a day before the watermark: records posted late on the watermark day would
# otherwise fall between two runs.
OVERLAP = timedelta(days=1)


class Ingestor:
    def __init__(
        self,
        store: Store,
        bedrock,
        http: httpx.Client,
        embedding_model: str,
        extraction_model: str,
    ):
        self.store = store
        self.bedrock = bedrock
        self.http = http
        self.embedding_model = embedding_model
        self.extraction_model = extraction_model
        self.pool = ThreadPoolExecutor(max_workers=WORKERS)

    def embed_changed(self, source: str, items: list[tuple[str, dict[str, str]]]) -> int:
        """Embed and store the chunks whose text changed. items: (source_id, {section: text})."""
        existing = self.store.chunk_hashes(source, [source_id for source_id, _ in items])
        todo = [
            (source_id, section, text, content_hash(text))
            for source_id, chunks in items
            for section, text in chunks.items()
            if existing.get((source_id, section)) != content_hash(text)
        ]
        vectors = self.pool.map(lambda t: embed(self.bedrock, self.embedding_model, t[2]), todo)
        self.store.upsert_chunks(
            [
                {
                    "source": source,
                    "source_id": source_id,
                    "section": section,
                    "text": text,
                    "embedding": vector,
                    "model": self.embedding_model,
                    "content_hash": digest,
                }
                for (source_id, section, text, digest), vector in zip(todo, vectors, strict=True)
            ]
        )
        return len(todo)

    def extract_changed(self, trials: list[ctgov.ParsedTrial]) -> int:
        """Extract and store eligibility for trials whose criteria changed. Returns the count
        stored. A trial the model fails on is skipped and retried by the next run, since
        its hash is not stored; search treats it as unrestricted meanwhile."""
        texts = {
            t.row["nct_id"]: eligibility.criteria_text(
                t.row["title"], t.row["eligibility_criteria"]
            )
            for t in trials
            if t.row["eligibility_criteria"]
        }
        # Requirements read from criteria the trial no longer lists would keep filtering it.
        dropped = [t.row["nct_id"] for t in trials if t.row["nct_id"] not in texts]
        if dropped:
            self.store.delete_eligibility(dropped)
        existing = self.store.eligibility_hashes(list(texts))
        todo = [
            (nct_id, text, content_hash(text))
            for nct_id, text in texts.items()
            if existing.get(nct_id) != content_hash(text)
        ]

        def attempt(nct_id: str, text: str) -> eligibility.Eligibility | None:
            try:
                return eligibility.extract(self.bedrock, self.extraction_model, text)
            except Exception:
                logger.exception("eligibility extraction failed", extra={"nct_id": nct_id})
                return None

        results = self.pool.map(lambda t: attempt(t[0], t[1]), todo)
        rows = [
            result.model_dump()
            | {"nct_id": nct_id, "model": self.extraction_model, "criteria_hash": digest}
            for (nct_id, _, digest), result in zip(todo, results, strict=True)
            if result is not None
        ]
        self.store.upsert_eligibility(rows)
        return len(rows)

    def trials(self, since: date | None) -> dict:
        counts = {"fetched": 0, "upserted": 0, "embedded": 0, "extracted": 0}
        for studies in batched(ctgov.fetch_studies(self.http, since), TRIAL_BATCH, strict=False):
            parsed = [ctgov.parse_study(s) for s in studies]
            list(
                self.pool.map(
                    lambda t: self.store.put_raw(
                        trial_raw_key(t.row["nct_id"]),
                        json.dumps(t.raw).encode(),
                        "application/json",
                    ),
                    parsed,
                )
            )
            self.store.upsert_trials(parsed)
            counts["embedded"] += self.embed_changed(
                "trial", [(t.row["nct_id"], t.chunks) for t in parsed]
            )
            counts["extracted"] += self.extract_changed(parsed)
            counts["fetched"] += len(studies)
            counts["upserted"] += len(parsed)
            logger.info("trials batch", extra=counts)
        return counts

    def papers(self, eutils: pubmed.EUtils, pmids: list[str]) -> dict:
        counts = {"fetched": 0, "upserted": 0, "embedded": 0}
        for batch in batched(pmids, pubmed.FETCH_BATCH, strict=False):
            parsed = [pubmed.parse_article(a) for a in eutils.fetch(list(batch))]
            list(
                self.pool.map(
                    lambda p: self.store.put_raw(
                        paper_raw_key(p.row["pmid"]), p.raw, "application/xml"
                    ),
                    parsed,
                )
            )
            self.store.upsert_papers(parsed)
            counts["embedded"] += self.embed_changed(
                "paper", [(p.row["pmid"], p.chunks) for p in parsed]
            )
            counts["fetched"] += len(batch)
            counts["upserted"] += len(parsed)
            logger.info("papers batch", extra=counts)
        return counts

    def run(self, source: str, work, advances_watermark: bool = True) -> dict:
        """Record an ingest_runs row around `work`, a zero-argument callable returning counts.
        A run that does not re-read revised records must not advance the watermark, or
        the next update would skip revisions since the last one."""
        started = date.today()
        self.store.wake()
        run_id = self.store.start_run(source)
        try:
            counts = work()
        except Exception as error:
            self.store.finish_run(run_id, "failed", None, error=repr(error)[:2000])
            raise
        watermark = started if advances_watermark else None
        self.store.finish_run(run_id, "succeeded", watermark, **counts)
        logger.info("ingest run finished", extra={"source": source, **counts})
        return counts

    def update_trials(self) -> dict:
        self.store.wake()
        watermark = self.store.last_watermark("ctgov")
        return self.run("ctgov", lambda: self.trials(watermark - OVERLAP if watermark else None))

    def update_papers(self, eutils: pubmed.EUtils) -> dict:
        """Papers added or revised since the last successful run. Requires a prior backfill."""
        self.store.wake()
        watermark = self.store.last_watermark("pubmed")
        if watermark is None:
            raise RuntimeError("no successful pubmed run yet; run scripts/ingest_backfill.py")
        start, end = watermark - OVERLAP, date.today()
        pmids = sorted(
            set(eutils.search(start, end, "edat")) | set(eutils.search(start, end, "mdat"))
        )
        return self.run("pubmed", lambda: self.papers(eutils, pmids))

    def backfill_papers(self, eutils: pubmed.EUtils, first_year: int) -> dict:
        """Every matching paper, one publication year at a time to stay under ESearch's cap."""

        def work() -> dict:
            total = {"fetched": 0, "upserted": 0, "embedded": 0}
            for year in range(first_year, date.today().year + 1):
                pmids = eutils.search(date(year, 1, 1), date(year, 12, 31), "pdat")
                logger.info("papers year", extra={"year": year, "pmids": len(pmids)})
                for key, value in self.papers(eutils, pmids).items():
                    total[key] += value
            return total

        return self.run("pubmed", work)

    def reconcile_papers(self, eutils: pubmed.EUtils, first_year: int) -> dict:
        """Match stored papers to PubMed's full result set: fetch what is missing, delete
        what PubMed no longer returns (withdrawn, or no longer matching TERM).

        Sliced by entry date, which every record has. The backfill sliced by publication
        date and missed records dated past its last slice or between two slices.
        """

        def work() -> dict:
            upstream: set[str] = set()
            for year in range(first_year, date.today().year + 1):
                upstream.update(eutils.search(date(year, 1, 1), date(year, 12, 31), "edat"))
            stored = self.store.paper_ids()
            missing, gone = sorted(upstream - stored), sorted(stored - upstream)
            logger.info("papers reconcile", extra={"missing": len(missing), "gone": len(gone)})
            if len(gone) > MAX_REMOVED_SHARE * len(stored):
                raise RuntimeError(
                    f"{len(gone)} of {len(stored)} stored papers not in PubMed; refusing to delete"
                )
            counts = self.papers(eutils, missing)
            self.store.delete_papers(gone)
            return counts

        return self.run("pubmed", work, advances_watermark=False)
