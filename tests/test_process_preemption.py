"""M2.3 tests — ProcessPreemptibleDispatcher (process-based, hard kill).

Covers:
  - ProcessPreemptibleDispatcher wraps an ActionDispatcher and forwards
    register/allowed_actions/authorize/idempotency_store.
  - Fast handler completes normally (no preemption).
  - Slow handler is preempted and KILLED (process terminated).
  - Infinite loop handler is preempted (true hard kill — the test
    that proves M2.3 closes the M2.2 orphan-thread limitation).
  - Handler that raises -> FAILED.
  - Per-call timeout override.
  - Pre-cancelled token -> immediate INDETERMINATE (no process spawn).
  - No timeout -> synchronous dispatch (backward compat).
  - default_timeout_seconds <= 0 rejected at construction.
  - Lambda handler -> dispatch returns FAILED (pickling error caught).

Documented limitation tested:
  - The handler MUST be a picklable top-level function. Lambdas raise
    at dispatch time (caught and returned as FAILED with a helpful
    message). This is the documented trade-off vs M2.2.

NOTE: the handler functions used here are defined in
tests/_process_handlers.py (a separate module) because they must be
importable by the child process on spawn-start-method platforms.
On fork (the default on Linux), the child inherits the parent's
memory, but picklable top-level functions are still required for
the Process(target=...) call to work reliably.
"""
import os
import sys
import time
import signal

import pytest

# Ensure the test handlers module is importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _process_handlers import (
    fast_handler, slow_handler, infinite_loop_handler, raising_handler, counter_handler,
)

from UPMP import (
    ActionDispatcher, ActionResult, ActionStatus, ClosedLoopOrchestrator,
    Constraint, Observation, ProcessState, VerificationPolicy, WorkflowStep, RunStatus,
)
from UPMP.closed_loop import ActionRequest
from UPMP.cancellation import CancellationTokenSource
from UPMP.idempotency import InMemoryIdempotencyStore
from UPMP.process_preemption import ProcessPreemptibleDispatcher
from dataclasses import replace


def state(**values):
    observations = {
        key: value if isinstance(value, Observation) else Observation(value, source="test")
        for key, value in values.items()
    }
    observations.setdefault("_test_guard", Observation(True, source="fixture"))
    return ProcessState("test.domain", "1", observations)


def policy(name, *constraints):
    if not constraints:
        constraints = (Constraint("_test_guard", "eq", True),)
    return VerificationPolicy(name, "1", constraints)


class TestForwarding:
    def test_register_forwards_to_wrapped(self):
        base = ActionDispatcher({"act"})
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=2.0)
        preemptible.register("act", fast_handler)
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        assert result.status == ActionStatus.SUCCEEDED

    def test_allowed_actions_forwarded(self):
        base = ActionDispatcher({"a", "b"})
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=2.0)
        assert preemptible.allowed_actions == base.allowed_actions

    def test_authorize_forwarded(self):
        auth = lambda req, cur: True
        base = ActionDispatcher({"act"}, authorize=auth)
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=2.0)
        assert preemptible.authorize is auth

    def test_idempotency_store_forwarded(self):
        store = InMemoryIdempotencyStore()
        base = ActionDispatcher({"act"}, idempotency_store=store)
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=2.0)
        assert preemptible.idempotency_store is store

    def test_wrapped_property(self):
        base = ActionDispatcher({"act"})
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=2.0)
        assert preemptible.wrapped is base


class TestFastHandler:
    def test_fast_handler_completes_normally(self):
        base = ActionDispatcher({"act"})
        base.register("act", fast_handler)
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=2.0)
        start = time.monotonic()
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        elapsed = time.monotonic() - start
        assert result.status == ActionStatus.SUCCEEDED
        assert elapsed < 1.5  # subprocess overhead is small

    def test_no_timeout_dispatches_synchronously(self):
        base = ActionDispatcher({"act"})
        base.register("act", fast_handler)
        preemptible = ProcessPreemptibleDispatcher(base)  # default_timeout_seconds=None
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        assert result.status == ActionStatus.SUCCEEDED


class TestHardPreemption:
    def test_slow_handler_killed_within_timeout(self):
        """THE M2.3 capability: a slow handler is preempted AND killed.
        The dispatch returns within the timeout; the child process is
        terminated (not left running, unlike M2.2)."""
        base = ActionDispatcher({"slow"})
        base.register("slow", slow_handler)
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=0.2)
        start = time.monotonic()
        result = preemptible.dispatch(ActionRequest(action="slow"), state())
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        assert result.status == ActionStatus.INDETERMINATE
        assert "preempted" in result.message
        assert "killed" in result.message

    def test_infinite_loop_handler_killed(self):
        """An infinite loop handler (the worst case for M2.2) is
        preempted and killed via SIGKILL. The dispatch returns within
        a bounded time."""
        base = ActionDispatcher({"loop"})
        base.register("loop", infinite_loop_handler)
        preemptible = ProcessPreemptibleDispatcher(base,
                                                   default_timeout_seconds=0.1,
                                                   kill_grace_seconds=0.3)
        start = time.monotonic()
        result = preemptible.dispatch(ActionRequest(action="loop"), state())
        elapsed = time.monotonic() - start
        assert elapsed < 2.0  # bounded: 0.1 timeout + 0.3 kill grace + overhead
        assert result.status == ActionStatus.INDETERMINATE

    def test_per_call_timeout_override(self):
        base = ActionDispatcher({"slow"})
        base.register("slow", slow_handler)
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=10.0)
        start = time.monotonic()
        result = preemptible.dispatch(ActionRequest(action="slow"), state(),
                                      timeout_seconds=0.1)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        assert result.status == ActionStatus.INDETERMINATE


class TestHandlerExceptions:
    def test_handler_that_raises_returns_failed(self):
        base = ActionDispatcher({"raise"})
        base.register("raise", raising_handler)
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=2.0)
        result = preemptible.dispatch(ActionRequest(action="raise"), state())
        assert result.status == ActionStatus.FAILED
        assert "RuntimeError" in result.message
        assert "boom from process handler" in result.message


class TestCancellationTokenPrecheck:
    def test_pre_cancelled_token_returns_immediately(self):
        source = CancellationTokenSource()
        source.cancel()
        base = ActionDispatcher({"act"})
        base.register("act", slow_handler)  # would hang if it ran
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=2.0)
        start = time.monotonic()
        result = preemptible.dispatch(
            ActionRequest(action="act", cancellation_token=source.token), state())
        elapsed = time.monotonic() - start
        assert elapsed < 0.05  # immediate
        assert result.status == ActionStatus.INDETERMINATE
        assert "cancellation token" in result.message


class TestValidation:
    def test_zero_timeout_rejected(self):
        base = ActionDispatcher({"act"})
        with pytest.raises(ValueError, match="default_timeout_seconds must be positive"):
            ProcessPreemptibleDispatcher(base, default_timeout_seconds=0)

    def test_negative_timeout_rejected(self):
        base = ActionDispatcher({"act"})
        with pytest.raises(ValueError, match="default_timeout_seconds must be positive"):
            ProcessPreemptibleDispatcher(base, default_timeout_seconds=-1.0)

    def test_negative_kill_grace_rejected(self):
        base = ActionDispatcher({"act"})
        with pytest.raises(ValueError, match="kill_grace_seconds cannot be negative"):
            ProcessPreemptibleDispatcher(base, default_timeout_seconds=1.0,
                                         kill_grace_seconds=-1.0)


class TestLambdaHandlerLimitation:
    def test_lambda_handler_returns_failed_not_hang(self):
        """LIMITATION: lambda handlers cannot be pickled for spawn.
        On fork, the lambda is inherited via fork, so it actually
        works — but on spawn it would fail. This test verifies the
        dispatcher doesn't hang on either platform; the result is
        either SUCCEEDED (fork) or FAILED (spawn)."""
        base = ActionDispatcher({"act"})
        base.register("act", lambda r, c: ActionResult(ActionStatus.SUCCEEDED, "lambda", c))
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=2.0)
        # This either works (fork) or returns FAILED (spawn). Either
        # way, it must not hang.
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        assert result.status in (ActionStatus.SUCCEEDED, ActionStatus.FAILED)


class TestOrchestratorIntegration:
    def test_process_preemptible_with_orchestrator_blocked(self):
        """End-to-end: a slow handler wrapped in ProcessPreemptibleDispatcher
        is preempted; the orchestrator sees INDETERMINATE and routes
        to BLOCKED (no on_indeterminate branch)."""
        base = ActionDispatcher({"act"})
        base.register("act", slow_handler)
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=0.1)
        step = WorkflowStep("s", "act", policy("pre"), policy("post"))
        start = time.monotonic()
        result = ClosedLoopOrchestrator([step], preemptible).run(state())
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        assert result.status == RunStatus.BLOCKED
        assert result.actions[0].status == ActionStatus.INDETERMINATE
        assert "preempted" in result.actions[0].message

    def test_process_preemptible_with_recovery_step(self):
        """A preempted handler routes via on_indeterminate to a recovery
        step that completes normally."""
        base = ActionDispatcher({"slow", "recover"})
        base.register("slow", slow_handler)
        base.register("recover", lambda r, c: ActionResult(
            ActionStatus.SUCCEEDED, "recovered",
            replace(c, observations={**c.observations,
                                      "done": Observation(True, source="result")})))
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=0.1)
        steps = [
            WorkflowStep("s1", "slow", policy("pre"), policy("post"),
                         on_indeterminate="s2"),
            WorkflowStep("s2", "recover", policy("pre"),
                         policy("post", Constraint("done", "eq", True))),
        ]
        result = ClosedLoopOrchestrator(steps, preemptible).run(state(done=False))
        assert result.status == RunStatus.COMPLETED
        assert result.actions[0].status == ActionStatus.INDETERMINATE
        assert result.actions[1].status == ActionStatus.SUCCEEDED


class TestProcessIsActuallyKilled:
    def test_no_orphan_process_after_preemption(self):
        """Verify that after preemption, the child process is actually
        dead (not left running as an orphan). This is the key property
        that distinguishes M2.3 from M2.2."""
        import multiprocessing
        base = ActionDispatcher({"slow"})
        base.register("slow", slow_handler)
        preemptible = ProcessPreemptibleDispatcher(base,
                                                   default_timeout_seconds=0.1,
                                                   kill_grace_seconds=0.3)
        # Dispatch with a very long-running handler.
        preemptible.dispatch(ActionRequest(action="slow"), state())
        # After dispatch returns, give the OS a moment to reap the process.
        time.sleep(0.1)
        # There should be no orphaned 'upmp-process-slow' child still running.
        # We can't easily enumerate child PIDs portably, but we can verify
        # the dispatcher's internal process is gone by checking the
        # multiprocessing.active_children() list.
        active = multiprocessing.active_children()
        upmp_children = [c for c in active if c.name.startswith("upmp-process")]
        assert upmp_children == [], "expected no orphaned upmp child processes, got: %s" % (
            [c.name for c in upmp_children],)
