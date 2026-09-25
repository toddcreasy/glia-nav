"""ClinicalTrials.gov API v2: fetch glioblastoma studies and parse them into rows."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date

import httpx

STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"
PAGE_SIZE = 200

# BACKGROUND references are literature the sponsor cited when designing the trial, not
# publications of the trial itself, so they would link a trial to papers about other work.
RESULT_REFERENCE_TYPES = {"RESULT", "DERIVED"}

AGE_UNITS = {"year": 1, "month": 12, "week": 52, "day": 365, "hour": 8760, "minute": 525600}


@dataclass
class ParsedTrial:
    row: dict
    sites: list[dict]
    pmids: list[str]
    chunks: dict[str, str]
    search_text: str
    raw: dict = field(repr=False)


def fetch_studies(client: httpx.Client, since: date | None = None) -> Iterator[dict]:
    """Yield raw study records, following nextPageToken to the end."""
    params = {"query.cond": "glioblastoma", "pageSize": PAGE_SIZE, "format": "json"}
    if since:
        params["filter.advanced"] = f"AREA[LastUpdatePostDate]RANGE[{since.isoformat()},MAX]"
    while True:
        response = client.get(STUDIES_URL, params=params)
        response.raise_for_status()
        page = response.json()
        yield from page["studies"]
        token = page.get("nextPageToken")
        if not token:
            return
        params["pageToken"] = token


def parse_date(value: str | None) -> date | None:
    """CT.gov dates are YYYY-MM-DD or month precision YYYY-MM; the latter maps to the 1st."""
    if not value:
        return None
    parts = [int(p) for p in value.split("-")]
    return date(parts[0], parts[1], parts[2] if len(parts) > 2 else 1)


def parse_age(value: str | None) -> float | None:
    """'18 Years' -> 18.0, '6 Months' -> 0.5. Anything unrecognised is treated as no limit."""
    if not value:
        return None
    number, _, unit = value.partition(" ")
    per_year = AGE_UNITS.get(unit.lower().rstrip("s"))
    if per_year is None:
        return None
    return round(float(number) / per_year, 2)


def parse_study(study: dict) -> ParsedTrial:
    p = study["protocolSection"]
    ident = p["identificationModule"]
    status = p["statusModule"]
    design = p.get("designModule", {})
    conditions = p.get("conditionsModule", {})
    arms = p.get("armsInterventionsModule", {})
    eligibility = p.get("eligibilityModule", {})
    description = p.get("descriptionModule", {})

    nct_id = ident["nctId"]
    title = ident.get("officialTitle") or ident["briefTitle"]
    summary = description.get("briefSummary")
    criteria = eligibility.get("eligibilityCriteria")
    interventions = [
        {"type": i.get("type"), "name": i.get("name")} for i in arms.get("interventions", [])
    ]
    condition_list = conditions.get("conditions", [])
    sponsor = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name")

    row = {
        "nct_id": nct_id,
        "title": title,
        "brief_summary": summary,
        "overall_status": status["overallStatus"],
        "phases": design.get("phases", []),
        "study_type": design.get("studyType"),
        "conditions": condition_list,
        "interventions": interventions,
        "eligibility_criteria": criteria,
        "min_age_years": parse_age(eligibility.get("minimumAge")),
        "max_age_years": parse_age(eligibility.get("maximumAge")),
        "sex": eligibility.get("sex"),
        "healthy_volunteers": eligibility.get("healthyVolunteers"),
        "sponsor": sponsor,
        "start_date": parse_date(status.get("startDateStruct", {}).get("date")),
        "primary_completion_date": parse_date(
            status.get("primaryCompletionDateStruct", {}).get("date")
        ),
        "last_update_posted": parse_date(status["lastUpdatePostDateStruct"]["date"]),
    }

    # Contacts are deliberately not carried over: they name individual people.
    sites = [
        {
            "facility": loc.get("facility"),
            "city": loc.get("city"),
            "state": loc.get("state"),
            "country": loc.get("country"),
            "status": loc.get("status"),
            "latitude": loc.get("geoPoint", {}).get("lat"),
            "longitude": loc.get("geoPoint", {}).get("lon"),
        }
        for loc in p.get("contactsLocationsModule", {}).get("locations", [])
    ]

    pmids = sorted(
        {
            ref["pmid"]
            for ref in p.get("referencesModule", {}).get("references", [])
            if ref.get("pmid") and ref.get("type") in RESULT_REFERENCE_TYPES
        }
    )

    intervention_names = [i["name"] for i in interventions if i["name"]]
    chunks = {
        "summary": "\n".join(
            filter(
                None,
                [
                    title,
                    "Conditions: " + ", ".join(condition_list) if condition_list else None,
                    "Interventions: " + ", ".join(intervention_names)
                    if intervention_names
                    else None,
                    summary,
                ],
            )
        )
    }
    if criteria:
        chunks["eligibility"] = f"{title}\nEligibility criteria:\n{criteria}"

    # Trials are often known by a nickname (EF-14, CheckMate 548) or a sponsor protocol
    # number rather than the NCT ID; those live only in these fields.
    aliases = [
        ident.get("acronym"),
        ident.get("orgStudyIdInfo", {}).get("id"),
        *(s.get("id") for s in ident.get("secondaryIdInfos", [])),
    ]
    search_text = " ".join(
        filter(
            None,
            [
                nct_id,
                *aliases,
                ident["briefTitle"],
                title,
                summary,
                " ".join(condition_list),
                " ".join(conditions.get("keywords", [])),
                " ".join(intervention_names),
                sponsor,
                # People ask for trials by place ("GBM trials in Boston"). Country is left
                # out: nearly every trial would match "United States".
                " ".join(sorted({s["city"] for s in sites if s["city"]})),
                " ".join(sorted({s["state"] for s in sites if s["state"]})),
            ],
        )
    )

    return ParsedTrial(
        row=row, sites=sites, pmids=pmids, chunks=chunks, search_text=search_text, raw=study
    )
