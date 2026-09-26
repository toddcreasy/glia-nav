import json
from types import SimpleNamespace

from glia_nav.agents.agent import (
    _report_tool_result,
    _report_tool_start,
    describe_results,
    describe_search,
)


def test_describe_search_names_the_patient_filters():
    line = describe_search(
        {
            "q": "glioblastoma",
            "kind": "trials",
            "recruiting": True,
            "setting": "recurrent",
            "recurrence": 2,
            "mgmt": "unmethylated",
            "prior_bevacizumab": "true",
            "kps": 70,
            "city": "Boston",
        }
    )
    assert line == (
        'Searching recruiting trials for "glioblastoma": recurrent, recurrence 2, '
        "MGMT unmethylated, prior bevacizumab, KPS 70, in Boston"
    )


def test_describe_search_without_filters():
    assert describe_search({"q": "TTFields"}) == 'Searching trials and papers for "TTFields"'


def test_describe_results_counts_what_came_back():
    result = {"content": [{"text": json.dumps({"trials": [{}, {}], "papers": [{}]})}]}
    assert describe_results(result) == "Found 2 trials and 1 paper"
    assert describe_results({"content": [{"text": "resuming"}]}) is None


def event(name, state, **extra):
    return SimpleNamespace(
        tool_use={"name": name, "input": {"q": "gbm"}}, invocation_state=state, **extra
    )


def test_hooks_report_only_when_a_turn_asks():
    heard = []
    _report_tool_start(
        event("glia-nav-backend___search_trials_and_papers", {"on_progress": heard.append})
    )
    _report_tool_start(event("AgentAnswer", {"on_progress": heard.append}))
    _report_tool_start(event("glia-nav-backend___search_trials_and_papers", {}))
    result = {"content": [{"text": json.dumps({"trials": [], "papers": []})}]}
    _report_tool_result(
        event(
            "glia-nav-backend___search_trials_and_papers",
            {"on_progress": heard.append},
            result=result,
        )
    )
    assert heard == ['Searching trials and papers for "gbm"', "Found 0 trials and 0 papers"]


def test_a_failed_search_says_it_will_retry():
    heard = []
    failed = {"status": "error", "content": [{"text": "503 resuming"}]}
    _report_tool_result(
        event(
            "glia-nav-backend___search_trials_and_papers",
            {"on_progress": heard.append},
            result=failed,
        )
    )
    assert heard == ["The search didn't answer, trying again"]
