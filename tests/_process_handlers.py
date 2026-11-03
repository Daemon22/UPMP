"""Top-level handler functions for M2.3 process-preemption tests.

These MUST be at module scope (not lambdas or closures) so they are
picklable for multiprocessing. Lambdas and closures raise PicklingError
when the parent tries to send them to the child process.
"""
import time
from UPMP.closed_loop import ActionResult, ActionStatus
from UPMP.contracts import Observation, ProcessState
from dataclasses import replace


def fast_handler(request, state):
    """Returns immediately with SUCCEEDED."""
    return ActionResult(ActionStatus.SUCCEEDED, "fast",
                        replace(state, observations={
                            **state.observations,
                            "done": Observation(True, source="result"),
                        }))


def slow_handler(request, state):
    """Sleeps 30 seconds — will always be preempted by any reasonable timeout."""
    time.sleep(30)
    return ActionResult(ActionStatus.SUCCEEDED, "should-not-see-this", state)


def infinite_loop_handler(request, state):
    """Truly infinite loop — cannot return without preemption."""
    while True:
        pass


def raising_handler(request, state):
    """Raises RuntimeError — should be caught and returned as FAILED."""
    raise RuntimeError("boom from process handler")


def recover_handler(request, state):
    """Module-level recovery action for spawn-based dispatch tests."""
    return ActionResult(
        ActionStatus.SUCCEEDED,
        "recovered",
        replace(state, observations={
            **state.observations,
            "done": Observation(True, source="result"),
        }),
    )


def _global_counter():
    """Helper — returns a counter that increments on each call.
    Used to verify the handler actually ran in the child process."""
    if not hasattr(_global_counter, "n"):
        _global_counter.n = 0
    _global_counter.n += 1
    return _global_counter.n


def counter_handler(request, state):
    """Increments a module-level counter and returns the count.
    Note: the counter lives in the CHILD process, so the parent can't
    directly observe it — but the returned count value proves the
    handler ran."""
    n = _global_counter()
    return ActionResult(ActionStatus.SUCCEEDED, "count=%d" % n, state)
