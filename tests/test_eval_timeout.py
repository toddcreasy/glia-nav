import time

import pytest
from evals.test_agent_evals import within


def test_a_prompt_answer_comes_back():
    assert within(1, lambda: "answer") == "answer"


def test_a_stalled_call_times_out_without_waiting_for_it():
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="no answer in 0.2 s"):
        within(0.2, lambda: time.sleep(30))
    assert time.monotonic() - started < 2


def test_the_calls_own_error_is_raised():
    def fails():
        raise ValueError("gateway refused")

    with pytest.raises(ValueError, match="gateway refused"):
        within(1, fails)
