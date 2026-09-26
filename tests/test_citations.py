import json
from types import SimpleNamespace

from glia_nav.agents.agent import (
    REWRITE_PREFIX,
    AgentAnswer,
    cited_ids,
    drop_unsourced,
    rewrite_request,
    run_agent,
    source_ids,
    tool_calls,
)


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


class FakeAgent:
    """Scripted stand-in for a Strands Agent: each call appends the prompt and a tool
    result to the conversation and returns the next scripted answer."""

    def __init__(self, answers, tool_payload):
        self.answers = list(answers)
        self.tool_payload = tool_payload
        self.messages = []
        self.prompts = []
        self.event_loop_metrics = SimpleNamespace(tool_metrics={})
        self.model = SimpleNamespace(get_config=lambda: {"model_id": "fake"})

    def __call__(self, prompt):
        self.prompts.append(prompt)
        self.messages.append({"role": "user", "content": [{"text": prompt}]})
        self.messages.append(tool_result(self.tool_payload))
        invocation = SimpleNamespace(
            usage={"inputTokens": 100, "outputTokens": 10}, cycles=[object()]
        )
        return SimpleNamespace(
            structured_output=AgentAnswer(answer=self.answers.pop(0)),
            metrics=SimpleNamespace(agent_invocations=[invocation]),
            stop_reason="end_turn",
        )


def test_sourced_answer_is_not_rewritten():
    agent = FakeAgent(["See NCT00916409."], SEARCH)
    reply = run_agent("Which trial is EF-14?", agent=agent)
    assert reply.answer == "See NCT00916409."
    assert len(agent.prompts) == 1
    assert reply.tokens == 110


def test_unsourced_answer_is_rewritten_from_the_results():
    agent = FakeAgent(["1. NCT00916409\n2. NCT99999999"], SEARCH)
    agent.answers.append("1. NCT00916409")
    reply = run_agent("Trials for recurrent GBM?", agent=agent)
    assert reply.answer == "1. NCT00916409"
    assert agent.prompts[1].startswith(REWRITE_PREFIX)
    assert "NCT99999999" in agent.prompts[1]
    assert reply.tokens == 220


def test_rewrite_that_still_cites_outside_the_results_loses_those_lines():
    agent = FakeAgent(["1. NCT00916409\n2. NCT99999999", "1. NCT00916409\n2. NCT99999999"], SEARCH)
    reply = run_agent("Trials for recurrent GBM?", agent=agent)
    assert reply.answer == "1. NCT00916409"


def test_rewrite_request_does_not_make_its_ids_sources():
    messages = [{"role": "user", "content": [{"text": rewrite_request({"NCT99999999"})}]}]
    assert source_ids(messages) == set()


def test_drop_unsourced_keeps_other_lines():
    answer = "Intro.\n1. NCT00916409 EF-14\n2. NCT99999999 invented\nAsk your team."
    assert drop_unsourced(answer, {"NCT99999999"}) == (
        "Intro.\n1. NCT00916409 EF-14\nAsk your team."
    )
