from types import SimpleNamespace

from evals.cases import Expected, SearchUsesFilters

from glia_nav.agents.agent import AgentReply, ToolCall

SEARCH = "glia-nav-backend___search_trials_and_papers"
WANTED = Expected(used_tool=True, filters=(("mgmt", "unmethylated"), ("kps", 70)))


def judge(*calls: ToolCall, expected: Expected = WANTED) -> bool:
    reply = AgentReply(answer="", used_tool=True, tool_calls=list(calls))
    return SearchUsesFilters().evaluate(SimpleNamespace(output=reply, metadata=expected))


def test_filters_on_one_search_call_pass():
    assert judge(ToolCall(name=SEARCH, input={"q": "gbm", "mgmt": "unmethylated", "kps": "70"}))


def test_attributes_left_in_the_query_fail():
    assert not judge(ToolCall(name=SEARCH, input={"q": "MGMT unmethylated KPS 70 glioblastoma"}))


def test_filters_split_across_calls_or_on_another_tool_fail():
    assert not judge(
        ToolCall(name=SEARCH, input={"q": "gbm", "mgmt": "unmethylated"}),
        ToolCall(name=SEARCH, input={"q": "gbm", "kps": 70}),
    )
    assert not judge(ToolCall(name="current_time", input={"mgmt": "unmethylated", "kps": 70}))


def test_cases_without_filters_pass():
    assert judge(expected=Expected(used_tool=True))
