import json
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import httpx
import pytest
from defusedxml import EntitiesForbidden

from glia_nav.ingest import ctgov, pubmed
from glia_nav.ingest.run import Ingestor
from glia_nav.ingest.store import param

FIXTURES = Path(__file__).parent / "fixtures"
STUDIES = json.loads((FIXTURES / "ctgov_studies.json").read_text())["studies"]
ARTICLES = list(ET.parse(FIXTURES / "pubmed_efetch.xml").getroot().iter("PubmedArticle"))


def test_parse_completed_trial():
    trial = ctgov.parse_study(STUDIES[0])
    row = trial.row
    assert row["nct_id"] == "NCT00916409"
    assert row["overall_status"] == "COMPLETED"
    assert row["phases"] == ["PHASE3"]
    assert row["min_age_years"] == 18.0
    assert row["max_age_years"] is None
    assert row["start_date"] == date(2009, 6, 1)
    assert row["last_update_posted"] == date(2017, 4, 10)
    assert {"type": "DEVICE", "name": "NovoTTF-100A device"} in row["interventions"]
    assert set(trial.chunks) == {"summary", "eligibility"}
    assert "NovoTTF-100A device" in trial.chunks["summary"]
    assert "NCT00916409" in trial.search_text


def test_trial_search_text_includes_aliases():
    # EF-14 is the sponsor's protocol number; the second trial has NIH and site ids.
    assert "EF-14" in ctgov.parse_study(STUDIES[0]).search_text
    other = ctgov.parse_study(STUDIES[1]).search_text
    assert "R37CA251978" in other
    assert "OCR44973" in other


def test_trial_search_text_includes_site_places():
    # The recruiting fixture trial has one site: Gainesville, Florida.
    text = ctgov.parse_study(STUDIES[1]).search_text
    assert "Gainesville" in text
    assert "Florida" in text
    assert "United States" not in text


def test_trial_links_only_result_references():
    # EF-14 lists five BACKGROUND papers and nine DERIVED ones, including its JAMA result.
    pmids = ctgov.parse_study(STUDIES[0]).pmids
    assert len(pmids) == 9
    assert "29260225" in pmids
    assert "15126372" not in pmids


def test_trial_sites_carry_no_contacts():
    trial = ctgov.parse_study(STUDIES[1])
    assert trial.sites[0]["status"] == "RECRUITING"
    assert trial.sites[0]["latitude"] is not None
    assert all("contacts" not in site for site in trial.sites)


def test_parse_age_units():
    assert ctgov.parse_age("6 Months") == 0.5
    assert ctgov.parse_age("80 Years") == 80.0
    assert ctgov.parse_age("N/A") is None
    assert ctgov.parse_age(None) is None


def test_fetch_studies_follows_page_tokens():
    pages = {
        None: {"studies": [{"n": 1}], "nextPageToken": "t2"},
        "t2": {"studies": [{"n": 2}]},
    }
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert list(ctgov.fetch_studies(client, since=date(2026, 9, 1))) == [{"n": 1}, {"n": 2}]
    assert seen[0]["filter.advanced"] == "AREA[LastUpdatePostDate]RANGE[2026-09-01,MAX]"


def test_eutils_retries_transient_errors(monkeypatch):
    monkeypatch.setattr(pubmed, "BACKOFF", 0)
    statuses = iter([500, 429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            next(statuses), json={"esearchresult": {"count": "1", "idlist": ["7"]}}
        )

    eutils = pubmed.EUtils(httpx.Client(transport=httpx.MockTransport(handler)))
    eutils.interval = 0
    assert eutils.search(date(2020, 1, 1), date(2020, 12, 31), "pdat") == ["7"]


def test_eutils_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(pubmed, "BACKOFF", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    eutils = pubmed.EUtils(httpx.Client(transport=httpx.MockTransport(handler)))
    eutils.interval = 0
    with pytest.raises(httpx.HTTPStatusError):
        eutils.search(date(2020, 1, 1), date(2020, 12, 31), "pdat")
    assert len(calls) == pubmed.RETRIES


def test_efetch_retries_truncated_xml(monkeypatch):
    monkeypatch.setattr(pubmed, "BACKOFF", 0)
    bodies = iter(
        [b"<PubmedArticleSet><PubmedArticle>", (FIXTURES / "pubmed_efetch.xml").read_bytes()]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(bodies))

    eutils = pubmed.EUtils(httpx.Client(transport=httpx.MockTransport(handler)))
    eutils.interval = 0
    assert len(list(eutils.fetch(["29260225", "15758009"]))) == 2


def test_efetch_refuses_entity_expansion():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        b"<PubmedArticleSet><PubmedArticle>&b;</PubmedArticle></PubmedArticleSet>"
    )
    eutils = pubmed.EUtils(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=bomb)))
    )
    eutils.interval = 0
    with pytest.raises(EntitiesForbidden):
        list(eutils.fetch(["1"]))


def test_parse_article_with_trial_link():
    paper = pubmed.parse_article(ARTICLES[0])
    row = paper.row
    assert row["pmid"] == "29260225"
    assert row["title"].startswith("Effect of Tumor-Treating Fields")
    assert row["abstract"].startswith("IMPORTANCE: ")
    assert row["pub_date"] == date(2017, 12, 19)
    assert row["doi"] == "10.1001/jama.2017.18718"
    assert row["pmcid"] == "PMC5820703"
    assert "Randomized Controlled Trial" in row["pub_types"]
    # The record lists the accession twice.
    assert paper.nct_ids == ["NCT00916409"]
    assert set(paper.chunks) == {"abstract"}


def test_parse_article_without_trial_link():
    paper = pubmed.parse_article(ARTICLES[1])
    assert paper.row["pmid"] == "15758009"
    assert paper.row["pmcid"] is None
    assert paper.nct_ids == []


def test_pub_date_formats():
    def parse(xml: str) -> date | None:
        return pubmed.parse_pub_date(ET.fromstring(xml))

    assert parse("<PubDate><Year>2019</Year><Month>Sep</Month></PubDate>") == date(2019, 9, 1)
    assert parse("<PubDate><Year>2019</Year><Month>09</Month><Day>3</Day></PubDate>") == date(
        2019, 9, 3
    )
    assert parse("<PubDate><MedlineDate>2017 Dec-2018 Jan</MedlineDate></PubDate>") == date(
        2017, 1, 1
    )
    assert parse("<PubDate><Year>2020</Year><Season>Spring</Season></PubDate>") == date(2020, 1, 1)


def test_title_keeps_inline_markup_text():
    article = ET.fromstring(
        "<PubmedArticle><MedlineCitation><PMID>1</PMID><Article>"
        "<ArticleTitle><i>IDH1</i> mutant glioma</ArticleTitle>"
        "</Article></MedlineCitation></PubmedArticle>"
    )
    paper = pubmed.parse_article(article)
    assert paper.row["title"] == "IDH1 mutant glioma"
    assert paper.chunks == {}


def test_param_types():
    assert param("a", None) == {"name": "a", "value": {"isNull": True}}
    assert param("a", True) == {"name": "a", "value": {"booleanValue": True}}
    assert param("a", 3) == {"name": "a", "value": {"longValue": 3}}
    assert param("a", ["x"]) == {"name": "a", "value": {"stringValue": '["x"]'}}
    assert param("a", date(2026, 1, 2)) == {
        "name": "a",
        "typeHint": "DATE",
        "value": {"stringValue": "2026-01-02"},
    }


class FakeStore:
    def __init__(self, hashes):
        self.hashes = hashes
        self.chunks = []

    def chunk_hashes(self, source, source_ids):
        return self.hashes

    def upsert_chunks(self, rows):
        self.chunks.extend(rows)


def test_unchanged_chunks_are_not_reembedded(monkeypatch):
    from glia_nav.ingest import embed, run

    embedded = []
    monkeypatch.setattr(run, "embed", lambda _b, _m, text: embedded.append(text) or [0.0])
    store = FakeStore({("p1", "abstract"): embed.content_hash("same")})
    ingestor = Ingestor(store, bedrock=None, http=None, embedding_model="m", extraction_model="x")

    count = ingestor.embed_changed(
        "paper", [("p1", {"abstract": "same"}), ("p2", {"abstract": "new"})]
    )

    assert count == 1
    assert embedded == ["new"]
    assert [c["source_id"] for c in store.chunks] == ["p2"]
