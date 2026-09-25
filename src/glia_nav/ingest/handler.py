"""Scheduled ingestion: pull each day's changes from ClinicalTrials.gov and PubMed.

Runs as a Lambda on a daily EventBridge schedule (IngestStack). It sits outside the VPC
on purpose: both sources are on the public internet, and Aurora is reached through the
Data API like everything else, so it needs no network route to the cluster.
"""

import logging

import boto3
import httpx
from botocore.config import Config

from glia_nav.config import IngestSettings
from glia_nav.ingest.pubmed import EUtils
from glia_nav.ingest.run import Ingestor
from glia_nav.ingest.store import Store
from glia_nav.logging_config import configure_logging

logger = logging.getLogger("glia_nav.ingest")


def handler(event, context) -> dict:
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

    results, failed = {}, []
    with httpx.Client(timeout=120, follow_redirects=True) as http:
        ingestor = Ingestor(store, bedrock, http, settings.embedding_model, settings.model_small)
        updates = {
            "ctgov": ingestor.update_trials,
            "pubmed": lambda: ingestor.update_papers(EUtils(http, api_key)),
        }
        # One source failing should not cost the other its update.
        for source, update in updates.items():
            try:
                results[source] = update()
            except Exception:
                logger.exception("ingest update failed", extra={"source": source})
                failed.append(source)

    if failed:
        # A raised error marks the invocation failed, which is what the alarm counts.
        raise RuntimeError(f"ingest update failed for {', '.join(failed)}")
    return results
