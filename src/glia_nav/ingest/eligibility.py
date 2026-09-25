"""Structured eligibility: the requirements patient matching filters on, read out of a
trial's free-text criteria by the small model tier.

The model is forced to answer through one tool whose input schema is the record, so the
reply is always JSON in that shape. Anything it cannot place is left null, which search
treats as no restriction: a misread trial should show up too often, never go missing.
The IDH, MGMT, and bevacizumab answers each need a quote from the criteria, and one whose
quote is not in the text, or does not name what it answers, is dropped: on a sample, the
model named an IDH status for a trial that never mentions IDH, once with no quote and
once quoting an unrelated line. So is one whose quote limits it to a cohort or arm, which
the model stored as trial-wide despite being told not to.

IDH and bevacizumab are asked as the facts behind the answer (every status allowed, the
kind of rule) and mapped here. Asked for the answer directly, the model picked one status
of two allowed and read washouts as exclusions.
"""

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

# Criteria past this are lab values and contraception rules; the requirements this reads
# are stated near the top. Keeps a rare 40k-character trial from costing ten normal ones.
MAX_CHARS = 20_000
MAX_TOKENS = 600

INSTRUCTIONS = """You read the eligibility criteria of a glioblastoma clinical trial and \
record the requirements a patient would be matched on. Call record_eligibility once.

Set a field only when the criteria or title state the requirement outright. Use null when \
the trial accepts either value, does not mention it, or you are unsure. Never infer a \
requirement from what the trial is testing. A requirement that applies to only some \
cohorts, arms, or parts of the trial is not a requirement of the trial: use null. For \
idh_allowed, mgmt, and bevacizumab_rule, copy the criterion that states the requirement \
word for word into the matching _quote field.

- setting: "newly_diagnosed" if only newly diagnosed patients can enroll, "recurrent" if \
only recurrent or progressive disease can enroll, null if both or unstated.
- max_recurrence: for recurrent trials, the highest recurrence allowed, e.g. 1 for "first \
recurrence only", 2 for "first or second recurrence". null if no limit is stated.
- idh_allowed: every IDH status an eligible tumor may have. ["wildtype", "mutant"] when \
the trial takes both, e.g. "glioblastoma (IDH-wildtype) or grade 4 IDH-mutant \
astrocytoma". null if the criteria do not mention IDH.
- mgmt: "methylated" or "unmethylated" only if the criteria require that MGMT promoter \
status.
- bevacizumab_rule: what the criteria say about bevacizumab (Avastin) or other \
anti-VEGF therapy. "any_prior_use_excluded" if having ever received it rules a patient \
out. "recent_use_excluded" if only use within a time window does, e.g. "within 4 weeks", \
"< 30 days prior", "in the last four weeks". "current_use_excluded" if only ongoing use \
does, e.g. "require active bevacizumab therapy at enrollment". "prior_use_required" if \
the patient must have received it or progressed on it. null if not mentioned.
- stated_min_kps: the minimum Karnofsky or Lansky performance status, as written.
- stated_max_ecog: the maximum ECOG or WHO performance status, as written, e.g. 2 for \
"ECOG 0-2"."""


class Eligibility(BaseModel):
    setting: Literal["newly_diagnosed", "recurrent"] | None = None
    max_recurrence: int | None = Field(default=None, ge=1)
    idh: Literal["wildtype", "mutant"] | None = None
    mgmt: Literal["methylated", "unmethylated"] | None = None
    prior_bevacizumab: Literal["excluded", "required"] | None = None
    min_kps: int | None = Field(default=None, ge=0, le=100)


class Reading(BaseModel):
    """What the model is asked for. Performance status is taken as written and converted
    here: asked to convert ECOG itself, the model got the arithmetic wrong."""

    setting: Literal["newly_diagnosed", "recurrent"] | None = None
    max_recurrence: int | None = Field(default=None, ge=1)
    idh_allowed: list[Literal["wildtype", "mutant"]] | None = None
    idh_quote: str | None = None
    mgmt: Literal["methylated", "unmethylated"] | None = None
    mgmt_quote: str | None = None
    bevacizumab_rule: (
        Literal[
            "any_prior_use_excluded",
            "recent_use_excluded",
            "current_use_excluded",
            "prior_use_required",
        ]
        | None
    ) = None
    bevacizumab_quote: str | None = None
    stated_min_kps: int | None = Field(default=None, ge=0, le=100)
    stated_max_ecog: int | None = Field(default=None, ge=0, le=4)


# A quote naming a subset of the trial, e.g. "(only for Dose Expansion Cohort)".
SUBSET = re.compile(r"\b(cohorts?|arms?|groups?|parts?)\b|\bonly for\b", re.I)

BEVACIZUMAB = {"any_prior_use_excluded": "excluded", "prior_use_required": "required"}


def normalize(text: str) -> str:
    """CT.gov escapes markdown characters (\\>), which a copied quote may leave out."""
    return re.sub(r"\s+", " ", text.replace("\\", "")).strip().lower()


# The lowest Karnofsky score each ECOG grade covers.
ECOG_TO_KPS = {0: 90, 1: 70, 2: 50, 3: 30, 4: 10}


def to_eligibility(reading: Reading, text: str) -> Eligibility:
    """Drops quoted answers whose quote is not in text. When a trial gives both performance
    scales, the looser one wins: search must not drop a patient one of the trial's own
    criteria admits."""
    source = normalize(text)

    def quoted(quote: str | None, names: str) -> bool:
        return bool(
            quote
            and normalize(quote) in source
            and re.search(names, quote, re.I)
            and not SUBSET.search(quote)
        )

    idh = set(reading.idh_allowed or [])
    floors = [
        kps
        for kps in (
            reading.stated_min_kps,
            ECOG_TO_KPS.get(reading.stated_max_ecog)
            if reading.stated_max_ecog is not None
            else None,
        )
        if kps is not None
    ]
    return Eligibility(
        setting=reading.setting,
        max_recurrence=reading.max_recurrence,
        idh=idh.pop() if len(idh) == 1 and quoted(reading.idh_quote, r"\bIDH") else None,
        mgmt=reading.mgmt if quoted(reading.mgmt_quote, r"\bMGMT") else None,
        prior_bevacizumab=BEVACIZUMAB.get(reading.bevacizumab_rule)
        if quoted(reading.bevacizumab_quote, r"bevacizumab|avastin|VEGF|angiogen")
        else None,
        min_kps=min(floors) if floors else None,
    )


TOOL = {
    "toolSpec": {
        "name": "record_eligibility",
        "description": "Record the trial's matching requirements.",
        "inputSchema": {"json": Reading.model_json_schema()},
    }
}


def criteria_text(title: str, criteria: str) -> str:
    """What the model reads, and what criteria_hash is taken over."""
    return f"Title: {title}\n\nEligibility criteria:\n{criteria[:MAX_CHARS]}"


def extract(bedrock, model_id: str, text: str) -> Eligibility:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": INSTRUCTIONS}],
        messages=[{"role": "user", "content": [{"text": text}]}],
        toolConfig={"tools": [TOOL], "toolChoice": {"tool": {"name": "record_eligibility"}}},
        inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0},
    )
    for block in response["output"]["message"]["content"]:
        if "toolUse" in block:
            return to_eligibility(Reading.model_validate(block["toolUse"]["input"]), text)
    raise ValueError(f"no tool call in reply: {json.dumps(response['output'])[:500]}")
