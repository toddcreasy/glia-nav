import json

from glia_nav.agents.agent import AgentAnswer, cited_ids, source_ids, tool_calls


def tool_result(payload: dict) -> dict:
    return {
        "role": "user",
        "content": [
            {"toolResult": {"toolUseId": "t1", "content": [{"text": json.dumps(payload)}]}}
        ],
    }


SEARCH = {
    "trials": [{"nct_id": "NCT00916409", "title": "EF-14"}],
    "papers": [{"pmid": "15758009", "doi": "10.1056/NEJMoa043330", "nct_ids": ["NCT00006353"]}],
}


def test_cited_ids_reads_nct_pmid_and_doi():
    answer = (
        "EF-14 (NCT00916409) and the Stupp trial, PMID: 15758009 "
        "(https://doi.org/10.1056/NEJMoa043330)."
    )
    assert cited_ids(answer) == {"NCT00916409", "PMID 15758009", "10.1056/nejmoa043330"}


def test_cited_doi_drops_markdown_emphasis():
    # The golden set failed a deploy on 2026-09-25 when the model bolded a DOI.
    assert cited_ids("DOI: **10.1007/s10143-026-04155-7**.") == {"10.1007/s10143-026-04155-7"}
    assert cited_ids("_10.1056/NEJMoa043330_") == {"10.1056/nejmoa043330"}


def test_source_ids_reads_search_json():
    assert source_ids([tool_result(SEARCH)]) == {
        "NCT00916409",
        "NCT00006353",
        "PMID 15758009",
        "10.1056/nejmoa043330",
    }


def test_source_ids_include_what_the_user_typed():
    messages = [{"role": "user", "content": [{"text": "Is NCT01234567 recruiting?"}]}]
    assert source_ids(messages) == {"NCT01234567"}


def test_source_ids_ignore_the_models_own_text():
    messages = [{"role": "assistant", "content": [{"text": "See NCT09999999."}]}]
    assert source_ids(messages) == set()


def test_an_invented_id_is_not_a_source():
    answer = "See NCT00916409 and PMID 11111111."
    assert cited_ids(answer) - source_ids([tool_result(SEARCH)]) == {"PMID 11111111"}


def test_tool_calls_keep_arguments_and_skip_structured_output():
    messages = [
        {"role": "user", "content": [{"text": "MGMT-unmethylated trials?"}]},
        {
            "role": "assistant",
            "content": [
                {"text": "Searching."},
                {
                    "toolUse": {
                        "toolUseId": "t1",
                        "name": "glia-nav-backend___search_trials_and_papers",
                        "input": {"q": "glioblastoma", "mgmt": "unmethylated"},
                    }
                },
            ],
        },
        tool_result(SEARCH),
        {
            "role": "assistant",
            "content": [
                {"toolUse": {"toolUseId": "t2", "name": AgentAnswer.__name__, "input": {}}}
            ],
        },
    ]
    calls = tool_calls(messages)
    assert [c.name for c in calls] == ["glia-nav-backend___search_trials_and_papers"]
    assert calls[0].input["mgmt"] == "unmethylated"
