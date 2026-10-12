import json

import pytest

from glia_nav.ingest import handler


class FakeIngestor:
    def __init__(self, *args):
        self.ran = []

    def update_trials(self):
        self.ran.append("ctgov")
        raise RuntimeError("ClinicalTrials.gov is down")

    def update_papers(self, eutils):
        self.ran.append("pubmed")
        return {"fetched": 1, "upserted": 1, "embedded": 0}


def test_one_failed_source_still_updates_the_other(monkeypatch):
    monkeypatch.setenv("DOCUMENTS_BUCKET", "test-bucket")
    ingestor = FakeIngestor()
    monkeypatch.setattr(handler, "Ingestor", lambda *args: ingestor)

    with pytest.raises(RuntimeError, match="ctgov"):
        handler.handler({}, None)

    assert ingestor.ran == ["ctgov", "pubmed"]


class FakeStore:
    def __init__(self, *args):
        pass

    def counts(self):
        return {"trials": 2268, "papers": 62077}


class FakeSsm:
    def __init__(self):
        self.put = {}

    def put_parameter(self, Name, Value, Overwrite):
        self.put = {"name": Name, "value": json.loads(Value), "overwrite": Overwrite}


def test_success_publishes_counts(monkeypatch):
    monkeypatch.setenv("DOCUMENTS_BUCKET", "test-bucket")
    ingestor = FakeIngestor()
    ingestor.update_trials = lambda: {"fetched": 0}
    monkeypatch.setattr(handler, "Ingestor", lambda *args: ingestor)
    monkeypatch.setattr(handler, "Store", FakeStore)
    ssm = FakeSsm()
    real_client = handler.boto3.client
    monkeypatch.setattr(
        handler.boto3,
        "client",
        lambda service, **kwargs: ssm if service == "ssm" else real_client(service, **kwargs),
    )

    handler.handler({}, None)

    assert ssm.put["name"] == "/glia-nav/index-stats"
    assert ssm.put["value"]["trials"] == 2268
    assert ssm.put["value"]["papers"] == 62077
    assert ssm.put["overwrite"] is True
