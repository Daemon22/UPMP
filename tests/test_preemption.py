"""M2.2 tests — PreemptibleDispatcher (thread-based handler preemption).

Covers:
  - PreemptibleDispatcher wraps an ActionDispatcher and forwards
    register/allowed_actions/authorize/idempotency_store.
  - default_timeout_seconds=None -> synchronous dispatch (backward
    compat with pre-M2.2 behavior).
  - default_timeout_seconds set -> handler that exceeds the timeout
    returns INDETERMINATE instead of blocking.
  - Per-call timeout_seconds override.
  - Pre-cancelled token -> immediate INDETERMINATE (no thread spawn).
  - default_timeout_seconds <= 0 rejected at construction.
  - Fast handler completes normally (no preemption).
  - Handler that raises -> FAILED (same contract as base dispatcher).
  - Idempotency store integration (cached result returned without
    spawning a worker thread — well, the worker runs but it returns
    the cached result fast).
  - End-to-end with orchestrator: a slow handler + PreemptibleDispatcher
    -> RunStatus.FAILED with INDETERMINATE action result.

Documented limitation tested:
  - The orphaned worker thread keeps running after preemption. We
    can't directly assert this from a test (the thread is daemonized
    and the test process exits), but we verify the dispatcher returns
    immediately rather than blocking on the slow handler.
"""
import time
import threading
from dataclasses import replace

import pytest

from UPMP import (
    ActionDispatcher, ActionResult, ActionStatus, ClosedLoopOrchestrator,
    Constraint, FindingStatus, Observation, ProcessState, VerificationPolicy,
    WorkflowStep, RunStatus,
)
from UPMP.closed_loop import ActionRequest
from UPMP.cancellation import CancellationTokenSource, NEVER_CANCELLED
from UPMP.idempotency import InMemoryIdempotencyStore
from UPMP.preemption import PreemptibleDispatcher


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


def success_dispatcher(action="act"):
    d = ActionDispatcher({action})
    d.register(action, lambda req, cur: ActionResult(
        ActionStatus.SUCCEEDED, "done",
        replace(cur, observations={**cur.observations,
                                   "done": Observation(True, source="result")})))
    return d


SUCCESS_STEP = WorkflowStep("s", "act", policy("pre"),
                            policy("post", Constraint("done", "eq", True)))


class TestForwarding:
    def test_register_forwards_to_wrapped(self):
        base = ActionDispatcher({"act"})
        preemptible = PreemptibleDispatcher(base)
        called = []
        preemptible.register("act", lambda req, cur: called.append(1) or ActionResult(
            ActionStatus.SUCCEEDED, "ok", cur))
        preemptible.dispatch(ActionRequest(action="act"), state())
        assert called == [1]

    def test_allowed_actions_forwarded(self):
        base = ActionDispatcher({"a", "b"})
        preemptible = PreemptibleDispatcher(base)
        assert preemptible.allowed_actions == base.allowed_actions

    def test_authorize_forwarded(self):
        auth = lambda req, cur: True
        base = ActionDispatcher({"act"}, authorize=auth)
        preemptible = PreemptibleDispatcher(base)
        assert preemptible.authorize is auth

    def test_idempotency_store_forwarded(self):
        store = InMemoryIdempotencyStore()
        base = ActionDispatcher({"act"}, idempotency_store=store)
        preemptible = PreemptibleDispatcher(base)
        assert preemptible.idempotency_store is store

    def test_wrapped_property(self):
        base = ActionDispatcher({"act"})
        preemptible = PreemptibleDispatcher(base)
        assert preemptible.wrapped is base


class TestSynchronousBackwardCompat:
    def test_no_timeout_dispatches_synchronously(self):
        base = ActionDispatcher({"act"})
        base.register("act", lambda r, c: ActionResult(ActionStatus.SUCCEEDED, "sync", c))
        preemptible = PreemptibleDispatcher(base)  # default_timeout_seconds=None
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        assert result.status == ActionStatus.SUCCEEDED
        assert result.message == "sync"

    def test_no_timeout_slow_handler_blocks(self):
        # Without a timeout, PreemptibleDispatcher delegates to the
        # wrapped dispatcher synchronously — a slow handler blocks.
        # This is the documented backward-compat behavior.
        base = ActionDispatcher({"act"})
        base.register("act", lambda r, c: (time.sleep(0.1),
            ActionResult(ActionStatus.SUCCEEDED, "slow", c))[1])
        preemptible = PreemptibleDispatcher(base)
        start = time.monotonic()
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        elapsed = time.monotonic() - start
        assert elapsed >= 0.1  # blocked
        assert result.status == ActionStatus.SUCCEEDED


class TestPreemption:
    def test_slow_handler_returns_indeterminate_within_timeout(self):
        base = ActionDispatcher({"act"})
        base.register("act", lambda r, c: (time.sleep(5.0),
            ActionResult(ActionStatus.SUCCEEDED, "should-not-see-this", c))[1])
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=0.1)
        start = time.monotonic()
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # did not wait the full 5 seconds
        assert result.status == ActionStatus.INDETERMINATE
        assert "preempted" in result.message
        assert "0.1" in result.message

    def test_per_call_timeout_override(self):
        base = ActionDispatcher({"act"})
        base.register("act", lambda r, c: (time.sleep(2.0),
            ActionResult(ActionStatus.SUCCEEDED, "slow", c))[1])
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=10.0)
        # Override to a shorter timeout for this call.
        start = time.monotonic()
        result = preemptible.dispatch(ActionRequest(action="act"), state(),
                                      timeout_seconds=0.1)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5
        assert result.status == ActionStatus.INDETERMINATE
        assert "0.1" in result.message

    def test_fast_handler_completes_normally(self):
        base = ActionDispatcher({"act"})
        base.register("act", lambda r, c: ActionResult(
            ActionStatus.SUCCEEDED, "fast", c))
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=1.0)
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        assert result.status == ActionStatus.SUCCEEDED
        assert result.message == "fast"

    def test_handler_that_raises_returns_failed(self):
        base = ActionDispatcher({"act"})
        base.register("act", lambda r, c: (_ for _ in ()).throw(RuntimeError("boom")))
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=1.0)
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        assert result.status == ActionStatus.FAILED
        assert "RuntimeError" in result.message
        assert "boom" in result.message


class TestCancellationTokenPrecheck:
    def test_pre_cancelled_token_returns_immediately(self):
        source = CancellationTokenSource()
        source.cancel()
        base = ActionDispatcher({"act"})
        called = []
        base.register("act", lambda r, c: (called.append(1),
            ActionResult(ActionStatus.SUCCEEDED, "should-not-run", c))[1])
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=1.0)
        start = time.monotonic()
        result = preemptible.dispatch(
            ActionRequest(action="act", cancellation_token=source.token), state())
        elapsed = time.monotonic() - start
        assert elapsed < 0.05  # immediate
        assert result.status == ActionStatus.INDETERMINATE
        assert "cancellation token" in result.message
        assert called == []  # handler never ran


class TestValidation:
    def test_zero_timeout_rejected(self):
        base = ActionDispatcher({"act"})
        with pytest.raises(ValueError, match="default_timeout_seconds must be positive"):
            PreemptibleDispatcher(base, default_timeout_seconds=0)

    def test_negative_timeout_rejected(self):
        base = ActionDispatcher({"act"})
        with pytest.raises(ValueError, match="default_timeout_seconds must be positive"):
            PreemptibleDispatcher(base, default_timeout_seconds=-1.0)


class TestIdempotencyStoreIntegration:
    def test_cached_result_returned_via_wrapper(self):
        store = InMemoryIdempotencyStore()
        base = ActionDispatcher({"act"}, idempotency_store=store)
        calls = []
        base.register("act", lambda r, c: (calls.append(1),
            ActionResult(ActionStatus.SUCCEEDED, "done", c))[1])
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=1.0)
        req = ActionRequest(action="act", idempotency_key="k1")
        r1 = preemptible.dispatch(req, state())
        r2 = preemptible.dispatch(req, state())
        assert len(calls) == 1  # handler called once
        assert r1.status == ActionStatus.SUCCEEDED
        assert r2.status == ActionStatus.SUCCEEDED
        # Cached result is the same object (store returns by identity).
        assert r1 is r2


class TestOrchestratorIntegration:
    def test_preemptible_dispatcher_with_orchestrator(self):
        """End-to-end: a slow handler wrapped in PreemptibleDispatcher
        is preempted; the orchestrator sees INDETERMINATE. With no
        on_indeterminate branch set, the run ends as BLOCKED
        (the orchestrator's routing for INDETERMINATE without a branch)."""
        base = ActionDispatcher({"act"})
        base.register("act", lambda r, c: (time.sleep(5.0),
            ActionResult(ActionStatus.SUCCEEDED, "should-not-see-this", c))[1])
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=0.1)
        # No on_indeterminate -> INDETERMINATE routes to BLOCKED.
        step = WorkflowStep("s", "act", policy("pre"), policy("post"))
        start = time.monotonic()
        result = ClosedLoopOrchestrator([step], preemptible).run(state())
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # did not wait 5s
        assert result.status == RunStatus.BLOCKED
        assert result.actions[0].status == ActionStatus.INDETERMINATE
        assert "preempted" in result.actions[0].message

    def test_preemptible_with_recovery_step(self):
        """A preempted handler routes via on_indeterminate to a
        recovery step that completes normally."""
        base = ActionDispatcher({"slow", "recover"})
        base.register("slow", lambda r, c: (time.sleep(5.0),
            ActionResult(ActionStatus.SUCCEEDED, "should-not-see", c))[1])
        base.register("recover", lambda r, c: ActionResult(
            ActionStatus.SUCCEEDED, "recovered",
            replace(c, observations={**c.observations,
                                      "done": Observation(True, source="result")})))
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=0.1)
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


class TestDocumentedLimitation:
    def test_orphan_thread_keeps_running(self):
        """LIMITATION (documented): a preempted handler thread keeps
        running. We can't directly assert it's still alive from the
        test process (the test will exit and clean it up), but we can
        verify the dispatcher returns immediately — proving the
        handler did NOT block the dispatch call."""
        base = ActionDispatcher({"act"})
        handler_finished = threading.Event()
        base.register("act", lambda r, c: (time.sleep(0.3),
            handler_finished.set(),
            ActionResult(ActionStatus.SUCCEEDED, "done", c))[1])
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=0.05)
        # Dispatch returns INDETERMINATE immediately.
        result = preemptible.dispatch(ActionRequest(action="act"), state())
        assert result.status == ActionStatus.INDETERMINATE
        assert not handler_finished.is_set()  # still running
        # Wait for the orphan to finish (so it doesn't leak into other tests).
        handler_finished.wait(timeout=1.0)
        assert handler_finished.is_set()
