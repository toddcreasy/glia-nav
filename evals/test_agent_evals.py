"""Runs the golden set against the agent before a deploy.

Every case costs real Bedrock tokens, so these are marked `evals` and excluded from
the default `pytest` run. Run them with `uv run pytest evals`.
"""

import pytest

from evals.cases import DATASET
from glia_nav.agents.agent import AgentReply, build_agent, run_agent

pytestmark = pytest.mark.evals


@pytest.fixture(scope="module")
def agent():
    return build_agent()


def test_golden_set(agent) -> None:
    def answer(prompt: str) -> AgentReply:
        # Each case is its own conversation; the agent otherwise accumulates turns.
        agent.messages = []
        return run_agent(prompt, agent=agent)

    report = DATASET.evaluate_sync(answer, max_concurrency=1)
    print(report)

    # The answer is the only way to tell a phrasing miss from a real regression.
    failures = [
        f"{case.name}: {name}\n  answer: {case.output.answer!r}"
        for case in report.cases
        for name, assertion in case.assertions.items()
        if not assertion.value
    ]
    # A case whose run raised has no answer to assert on and lands in report.failures,
    # not report.cases. Without this, an agent that crashes passes the gate.
    failures += [f"{case.name}: raised {case.error_message}" for case in report.failures]
    assert not failures, "golden cases failed:\n" + "\n".join(failures)
