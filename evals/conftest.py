"""Ends an eval run that left a stalled case behind.

Strands runs each agent call inside `with ThreadPoolExecutor()`, whose exit waits for the
call, and Python joins that executor's thread at shutdown. A stalled case therefore kept
the process alive after pytest had reported: on 2026-10-12 run 38291072963 printed its
result at 18:37 and held the deploy queue until the step's timeout at 18:48.
"""

import os
import sys

exit_status = 0


def pytest_sessionfinish(session, exitstatus):
    global exit_status
    exit_status = int(exitstatus)


def pytest_unconfigure(config):
    evals = sys.modules.get("evals.test_agent_evals")
    if evals is not None and evals.stalled:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_status)
