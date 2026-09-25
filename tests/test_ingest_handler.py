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
