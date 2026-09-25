"""PubMed E-utilities: find glioblastoma papers, fetch them as XML, parse them into rows."""

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date

import defusedxml.ElementTree as DefusedET
import httpx

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TERM = "(glioblastoma[tiab] OR glioblastoma[mh]) AND hasabstract"
FETCH_BATCH = 200
# ESearch will not page past the first 10,000 ids of a query. Callers slice by date so no
# single query gets near it; this guard makes a slice that does fail loudly, not silently.
ESEARCH_MAX = 9999
RETRIES = 5
BACKOFF = 2.0

MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


@dataclass
class ParsedPaper:
    row: dict
    nct_ids: list[str]
    chunks: dict[str, str]
    search_text: str
    raw: bytes = field(repr=False)


class EUtils:
    """Thin client that keeps to NCBI's rate limit: 3 requests/second, 10 with an API key."""

    def __init__(self, client: httpx.Client, api_key: str = ""):
        self.client = client
        self.base = {"tool": "glia-nav"} | ({"api_key": api_key} if api_key else {})
        self.interval = 0.1 if api_key else 0.34
        self.last = 0.0

    def _post(self, endpoint: str, data: dict) -> httpx.Response:
        # E-utilities returns sporadic 500s and 429s that succeed on a retry.
        for attempt in range(RETRIES):
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            response = self.client.post(f"{EUTILS}/{endpoint}", data=self.base | data)
            self.last = time.monotonic()
            if response.status_code != 429 and response.status_code < 500:
                break
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF * 2**attempt)
        response.raise_for_status()
        return response

    def search(self, start: date, end: date, datetype: str) -> list[str]:
        """PMIDs matching TERM whose `datetype` (edat: added, mdat: modified) is in range."""
        response = self._post(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": TERM,
                "datetype": datetype,
                "mindate": start.strftime("%Y/%m/%d"),
                "maxdate": end.strftime("%Y/%m/%d"),
                "retmax": ESEARCH_MAX,
                "retmode": "json",
            },
        )
        result = response.json()["esearchresult"]
        if int(result["count"]) > ESEARCH_MAX:
            raise ValueError(
                f"{result['count']} PMIDs for {datetype} {start}..{end}; slice the range smaller"
            )
        return result["idlist"]

    def fetch(self, pmids: list[str]) -> Iterator[ET.Element]:
        """Yield PubmedArticle elements. Book records and deletions are skipped."""
        for i in range(0, len(pmids), FETCH_BATCH):
            batch = pmids[i : i + FETCH_BATCH]
            # efetch sometimes answers 200 with a truncated body; that retries like a 5xx.
            for attempt in range(RETRIES):
                response = self._post(
                    "efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"}
                )
                try:
                    # Response bodies come off the network, so entity expansion is refused.
                    root = DefusedET.fromstring(response.content)
                    break
                except ET.ParseError:
                    if attempt == RETRIES - 1:
                        raise
                    time.sleep(BACKOFF * 2**attempt)
            yield from root.iter("PubmedArticle")


def text_of(element: ET.Element | None) -> str | None:
    """Full text of an element, including inline markup such as <i> and <sup>."""
    if element is None:
        return None
    return "".join(element.itertext()).strip() or None


def parse_pub_date(pub_date: ET.Element | None) -> date | None:
    if pub_date is None:
        return None
    year = pub_date.findtext("Year")
    if year is None:
        # MedlineDate is free text such as "2017 Dec-2018 Jan" or "2019 Spring".
        match = re.search(r"\d{4}", pub_date.findtext("MedlineDate") or "")
        return date(int(match.group()), 1, 1) if match else None
    month_text = (pub_date.findtext("Month") or "1").lower()
    month = int(month_text) if month_text.isdigit() else MONTHS.get(month_text[:3], 1)
    day = int(pub_date.findtext("Day") or 1)
    return date(int(year), month, day)


def parse_article(article: ET.Element) -> ParsedPaper:
    citation = article.find("MedlineCitation")
    art = citation.find("Article")
    pmid = citation.findtext("PMID")
    title = text_of(art.find("ArticleTitle")) or ""

    parts = []
    for section in art.findall("Abstract/AbstractText"):
        body = text_of(section)
        if body:
            label = section.get("Label")
            parts.append(f"{label}: {body}" if label else body)
    abstract = "\n".join(parts) or None

    revised = citation.find("DateRevised")
    ids = {i.get("IdType"): i.text for i in article.findall("PubmedData/ArticleIdList/ArticleId")}
    mesh = [m.findtext("DescriptorName") for m in citation.findall("MeshHeadingList/MeshHeading")]
    pub_types = [t.text for t in art.findall("PublicationTypeList/PublicationType")]

    nct_ids = sorted(
        {
            accession.text
            for bank in art.findall("DataBankList/DataBank")
            if bank.findtext("DataBankName") == "ClinicalTrials.gov"
            for accession in bank.findall("AccessionNumberList/AccessionNumber")
            if accession.text
        }
    )

    row = {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "journal": art.findtext("Journal/Title"),
        "pub_date": parse_pub_date(art.find("Journal/JournalIssue/PubDate")),
        "pub_types": pub_types,
        "mesh_terms": mesh,
        "doi": ids.get("doi"),
        "pmcid": ids.get("pmc"),
        "last_revised": parse_pub_date(revised),
    }

    chunks = {"abstract": f"{title}\n{abstract}"} if abstract else {}
    search_text = " ".join(filter(None, [pmid, title, abstract, " ".join(mesh)]))

    return ParsedPaper(
        row=row,
        nct_ids=nct_ids,
        chunks=chunks,
        search_text=search_text,
        raw=ET.tostring(article, encoding="utf-8"),
    )
