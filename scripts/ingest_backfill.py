"""Load every glioblastoma trial and paper. Run once locally; the scheduled job does the deltas.

Usage: uv run python scripts/ingest_backfill.py [trials|papers|all] [--from-year YEAR]

Safe to re-run or resume: every write is an upsert and unchanged chunks are not re-embedded.
--from-year resumes the paper backfill at that publication year after an interruption.
"""

import argparse

import boto3
import httpx
from botocore.config import Config

from glia_nav.config import IngestSettings
from glia_nav.ingest.pubmed import EUtils
from glia_nav.ingest.run import Ingestor
from glia_nav.ingest.store import Store
from glia_nav.logging_config import configure_logging

# PubMed indexes back to the 1940s; a year with no matches costs one quick request.
FIRST_YEAR = 1940


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("what", choices=["trials", "papers", "all"], nargs="?", default="all")
    parser.add_argument("--from-year", type=int, default=FIRST_YEAR)
    args = parser.parse_args()

    settings = IngestSettings()
    configure_logging(settings.log_level)
    retries = Config(retries={"mode": "adaptive", "max_attempts": 10})
    store = Store(
        settings,
        boto3.client("rds-data", region_name=settings.aws_region, config=retries),
        boto3.client("s3", region_name=settings.aws_region),
    )
    bedrock = boto3.client("bedrock-runtime", region_name=settings.aws_region, config=retries)
    api_key = settings.ncbi_api_key.get_secret_value() if settings.ncbi_api_key else ""

    with httpx.Client(timeout=120, follow_redirects=True) as http:
        ingestor = Ingestor(store, bedrock, http, settings.embedding_model, settings.model_small)
        if args.what in ("trials", "all"):
            print("trials", ingestor.run("ctgov", lambda: ingestor.trials(None)))
        if args.what in ("papers", "all"):
            print("papers", ingestor.backfill_papers(EUtils(http, api_key), args.from_year))


if __name__ == "__main__":
    main()
