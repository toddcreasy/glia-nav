import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from glia_nav.ingest import ctgov, eligibility
from glia_nav.ingest.embed import content_hash
from glia_nav.ingest.run import Ingestor

FIXTURES = Path(__file__).parent / "fixtures"
STUDIES = json.loads((FIXTURES / "ctgov_studies.json").read_text())["studies"]


class FakeBedrock:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"output": {"message": {"role": "assistant", "content": self.reply}}}


def tool_reply(**fields):
    return [{"toolUse": {"toolUseId": "t1", "name": "record_eligibility", "input": fields}}]


def test_extract_reads_the_tool_call():
    bedrock = FakeBedrock(
        tool_reply(
            setting="recurrent",
            bevacizumab_rule="any_prior_use_excluded",
            bevacizumab_quote="Prior treatment with   bevacizumab",
            stated_min_kps=60,
        )
    )
    text = "Title: x\n* Prior treatment with\nbevacizumab.\n* KPS \\>= 60"
    result = eligibility.extract(bedrock, "haiku", text)
    assert result == eligibility.Eligibility(
        setting="recurrent", prior_bevacizumab="excluded", min_kps=60
    )
    call = bedrock.calls[0]
    assert call["toolConfig"]["toolChoice"] == {"tool": {"name": "record_eligibility"}}
    assert call["inferenceConfig"]["temperature"] == 0


def test_extract_refuses_values_outside_the_schema():
    with pytest.raises(ValidationError):
        eligibility.extract(FakeBedrock(tool_reply(idh_allowed=["positive"])), "haiku", "Title: x")
    with pytest.raises(ValidationError):
        eligibility.extract(FakeBedrock(tool_reply(stated_min_kps=150)), "haiku", "Title: x")


def test_answers_without_a_matching_quote_are_dropped():
    text = "Title: x\n* IDH-wildtype glioblastoma"
    reading = eligibility.Reading(
        idh_allowed=["wildtype"],
        idh_quote="IDH-wildtype glioblastoma",
        mgmt="methylated",
        mgmt_quote="MGMT promoter methylated",
        bevacizumab_rule="any_prior_use_excluded",
    )
    result = eligibility.to_eligibility(reading, text)
    assert result.idh == "wildtype"
    assert result.mgmt is None
    assert result.prior_bevacizumab is None


def test_only_one_allowed_idh_status_and_lifetime_bevacizumab_rules_filter():
    text = "Title: x\n* IDH-wildtype GBM or IDH-mutant astrocytoma\n* No bevacizumab within 4 weeks"

    def read(**fields):
        return eligibility.to_eligibility(eligibility.Reading(**fields), text)

    both = read(idh_allowed=["wildtype", "mutant"], idh_quote="IDH-wildtype GBM or IDH-mutant")
    assert both.idh is None
    washout = read(
        bevacizumab_rule="recent_use_excluded", bevacizumab_quote="No bevacizumab within 4 weeks"
    )
    assert washout.prior_bevacizumab is None
    unrelated = read(idh_allowed=["wildtype"], idh_quote="Title: x")
    assert unrelated.idh is None


def test_answers_limited_to_a_cohort_are_dropped():
    text = "Title: x\n10. MGMT unmethylation (only for Dose Expansion Cohort)."
    reading = eligibility.Reading(
        mgmt="unmethylated", mgmt_quote="MGMT unmethylation (only for Dose Expansion Cohort)"
    )
    assert eligibility.to_eligibility(reading, text).mgmt is None


def test_performance_status_takes_the_looser_scale():
    def kps(**stated):
        return eligibility.to_eligibility(eligibility.Reading(**stated), "").min_kps

    assert kps(stated_max_ecog=1) == 70
    assert kps(stated_max_ecog=2) == 50
    assert kps(stated_min_kps=70, stated_max_ecog=2) == 50
    assert kps(stated_min_kps=60, stated_max_ecog=1) == 60
    assert kps(stated_max_ecog=0) == 90
    assert kps() is None


def test_extract_fails_without_a_tool_call():
    with pytest.raises(ValueError, match="no tool call"):
        eligibility.extract(FakeBedrock([{"text": "I cannot tell."}]), "haiku", "Title: x")


def test_criteria_text_is_capped():
    text = eligibility.criteria_text("T", "x" * (eligibility.MAX_CHARS + 500))
    assert text.count("x") == eligibility.MAX_CHARS


class FakeStore:
    def __init__(self, hashes):
        self.hashes = hashes
        self.rows = []
        self.deleted = []

    def eligibility_hashes(self, nct_ids):
        return self.hashes

    def upsert_eligibility(self, rows):
        self.rows.extend(rows)

    def delete_eligibility(self, nct_ids):
        self.deleted.extend(nct_ids)


def test_unchanged_criteria_are_not_reextracted():
    trials = [ctgov.parse_study(s) for s in STUDIES]
    first = trials[0].row
    unchanged = eligibility.criteria_text(first["title"], first["eligibility_criteria"])
    store = FakeStore({first["nct_id"]: content_hash(unchanged)})
    bedrock = FakeBedrock(tool_reply(setting="recurrent"))
    ingestor = Ingestor(store, bedrock, http=None, embedding_model="m", extraction_model="haiku")

    assert ingestor.extract_changed(trials) == len(trials) - 1
    assert first["nct_id"] not in {r["nct_id"] for r in store.rows}
    row = store.rows[0]
    assert row["setting"] == "recurrent"
    assert row["idh"] is None
    assert row["model"] == "haiku"


def test_failed_extraction_is_skipped_and_not_stored():
    trials = [ctgov.parse_study(s) for s in STUDIES]
    store = FakeStore({})
    ingestor = Ingestor(
        store,
        FakeBedrock(RuntimeError("throttled")),
        http=None,
        embedding_model="m",
        extraction_model="haiku",
    )

    assert ingestor.extract_changed(trials) == 0
    assert store.rows == []


def test_trial_without_criteria_loses_its_eligibility():
    trials = [ctgov.parse_study(s) for s in STUDIES]
    trials[0].row["eligibility_criteria"] = None
    store = FakeStore({})
    ingestor = Ingestor(
        store, FakeBedrock(tool_reply()), http=None, embedding_model="m", extraction_model="h"
    )

    ingestor.extract_changed(trials)

    assert store.deleted == [trials[0].row["nct_id"]]
