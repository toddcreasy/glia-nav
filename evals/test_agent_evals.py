"""Runs the golden set against the agent before a deploy.

Every case costs real Bedrock tokens, so these are marked `evals` and excluded from
the default `pytest` run. Run them with `uv run pytest evals`.
"""

import queue
import sys
import threading
import traceback
from collections.abc import Callable

import pytest

from evals.cases import DATASET
from glia_nav.agents.agent import AgentReply, build_agent, run_agent

pytestmark = pytest.mark.evals


# The slowest case takes about a minute, rewrite included. On 2026-09-25 a deploy's run
# stalled after 20 model calls and gave no sign of which case, until the step timed out.
CASE_TIMEOUT_S = 180


# Set once a case stalls; evals/conftest.py then ends the process instead of waiting on it.
stalled = False


def thread_stacks(skip: int) -> str:
    """Every thread's stack but the caller's. Strands runs the agent on its own executor
    thread and the MCP client on another, so the worker's stack alone ends at a wait."""
    names = {t.ident: t.name for t in threading.enumerate()}
    return "".join(
        f"--- thread {names.get(ident, ident)}\n" + "".join(traceback.format_stack(frame))
        for ident, frame in sys._current_frames().items()
        if ident != skip
    )


def within(seconds: float, call: Callable[[], AgentReply]) -> AgentReply:
    """call's result, or TimeoutError once seconds pass. The error carries every thread's
    stack, the only record of where the call was waiting."""
    global stalled
    outcome: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            outcome.put((True, call()))
        except Exception as exc:
            outcome.put((False, exc))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        ok, value = outcome.get(timeout=seconds)
    except queue.Empty:
        stalled = True
        stacks = thread_stacks(skip=threading.get_ident())
        raise TimeoutError(f"no answer in {seconds:g} s; threads:\n{stacks}") from None
    if not ok:
        raise value
    return value


def test_golden_set() -> None:
    agents = [build_agent()]

    def answer(prompt: str) -> AgentReply:
        agent = agents[-1]
        # Each case is its own conversation; the agent otherwise accumulates turns.
        agent.messages = []
        try:
            return within(CASE_TIMEOUT_S, lambda: run_agent(prompt, agent=agent))
        except TimeoutError:
            # The stalled call still holds that agent, so later cases get a fresh one.
            agents.append(build_agent())
            raise

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
