from fastapi.testclient import TestClient

from glia_nav.api.main import app

client = TestClient(app)


def test_health_is_static():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class FakeRds:
    class exceptions:
        class DatabaseResumingException(Exception):
            pass


def stubbed(monkeypatch, trials=None, papers=None):
    from glia_nav.api import main

    monkeypatch.setattr(main, "rds_data_client", lambda region: FakeRds())
    monkeypatch.setattr(main, "bedrock_client", lambda region: None)
    monkeypatch.setattr(main.hybrid, "query_vector", lambda *a: "[0]")
    monkeypatch.setattr(main.hybrid, "search_trials", trials or (lambda *a, **k: []))
    monkeypatch.setattr(main.hybrid, "search_papers", papers or (lambda *a, **k: []))


def test_search_needs_no_sign_in(monkeypatch):
    stubbed(monkeypatch)
    assert client.get("/search", params={"q": "vaccine"}).status_code == 200


def test_search_validates_input(monkeypatch):
    stubbed(monkeypatch)
    try:
        assert client.get("/search", params={"q": "x"}).status_code == 422
        assert client.get("/search", params={"q": "vaccine", "phase": "PHASE9"}).status_code == 422
        assert client.get("/search", params={"q": "vaccine", "limit": 500}).status_code == 422
        assert client.get("/search", params={"q": "vaccine", "idh": "positive"}).status_code == 422
        assert client.get("/search", params={"q": "vaccine", "kps": 110}).status_code == 422
        assert client.get("/search", params={"q": "vaccine", "recurrence": 0}).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_search_kind_selects_lists(monkeypatch):
    calls = []
    paper = {
        "pmid": "1",
        "title": "t",
        "journal": None,
        "pub_date": None,
        "pub_types": [],
        "doi": None,
        "snippet": None,
        "nct_ids": [],
        "score": 0.5,
    }
    stubbed(
        monkeypatch,
        trials=lambda *a, **k: calls.append("trials") or [],
        papers=lambda *a, **k: calls.append("papers") or [paper],
    )
    try:
        response = client.get("/search", params={"q": "vaccine", "kind": "papers"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert calls == ["papers"]
    assert response.json()["papers"][0]["pmid"] == "1"


def test_search_while_database_resumes(monkeypatch):
    def resuming(*args, **kwargs):
        raise FakeRds.exceptions.DatabaseResumingException

    stubbed(monkeypatch, trials=resuming)
    try:
        response = client.get("/search", params={"q": "vaccine"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json() == {"status": "resuming"}


def test_search_passes_eligibility_filters(monkeypatch):
    seen = {}
    trial = {
        "nct_id": "NCT06389591",
        "title": "t",
        "overall_status": "RECRUITING",
        "phases": [],
        "conditions": [],
        "sponsor": None,
        "min_age_years": 18.0,
        "max_age_years": None,
        "last_update_posted": "2026-01-02",
        "site_count": 0,
        "sites": [],
        "eligibility": {"setting": "recurrent", "prior_bevacizumab": "excluded", "min_kps": 60},
        "score": 0.5,
    }
    stubbed(monkeypatch, trials=lambda *a, **k: seen.update(k) or [trial])
    try:
        response = client.get(
            "/search",
            params={
                "q": "vaccine",
                "kind": "trials",
                "mgmt": "unmethylated",
                "recurrence": 1,
                "prior_bevacizumab": "false",
                "kps": 70,
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert seen["mgmt"] == "unmethylated"
    assert seen["recurrence"] == 1
    assert seen["prior_bevacizumab"] is False
    assert seen["kps"] == 70
    assert seen["idh"] is None
    hit = response.json()["trials"][0]
    assert hit["eligibility"]["prior_bevacizumab"] == "excluded"
    assert hit["eligibility"]["idh"] is None
