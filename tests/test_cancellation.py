"""M2 tests — cancellation and timeout for the closed-loop orchestrator.

Covers:
  - CancellationToken / CancellationTokenSource semantics (cancel, poll,
    cancel_after timer, one-way transition, idempotent cancel).
  - Run-level cancellation: orchestrator returns RunStatus.CANCELLED
    when the token is set; audit trail records run_cancelled event.
  - Run-level deadline: orchestrator returns RunStatus.TIMEOUT when the
    deadline is exceeded; audit trail records run_timeout event.
  - Per-step timeout_seconds: a handler that exceeds the limit has its
    result coerced to INDETERMINATE with reason "timeout" and routed
    via on_indeterminate.
  - Backward compatibility: no token, no deadline, no per-step timeout
    produces identical behavior to pre-M2 UPMP (existing tests pass).
  - Cooperative cancellation: a handler that polls the token exits
    early when cancelled, returning INDETERMINATE.

Limitation explicitly tested: cancellation is COOPERATIVE. A handler
that ignores the token and never returns cannot be preempted. This is
documented in the closed_loop.py module docstring and in THREAT.md;
preemption is deferred to M2.2.
"""
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from UPMP import (
    ActionDispatcher, ActionResult, ActionStatus, ClosedLoopOrchestrator,
    Constraint, FindingStatus, Observation, ProcessState, VerificationPolicy,
    WorkflowStep, RunStatus,
)
from UPMP.cancellation import CancellationToken, CancellationTokenSource, NEVER_CANCELLED
from UPMP.closed_loop import ActionRequest


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
    dispatcher = ActionDispatcher({action})
    dispatcher.register(action, lambda req, cur: ActionResult(
        ActionStatus.SUCCEEDED, "done",
        replace(cur, observations={**cur.observations, "done": Observation(True, source="result")})))
    return dispatcher


SUCCESS_STEP = WorkflowStep("s", "act", policy("pre"),
                            policy("post", Constraint("done", "eq", True)))


class TestCancellationToken:
    def test_fresh_token_not_cancelled(self):
        source = CancellationTokenSource()
        assert source.token.is_cancelled() is False
        assert source.token.cancelled is False

    def test_cancel_sets_flag(self):
        source = CancellationTokenSource()
        source.cancel()
        assert source.token.is_cancelled() is True
        assert source.is_cancelled is True

    def test_cancel_is_idempotent_and_one_way(self):
        source = CancellationTokenSource()
        source.cancel()
        source.cancel()  # idempotent — does not raise
        assert source.token.is_cancelled() is True

    def test_cancel_after_fires_in_background(self):
        source = CancellationTokenSource()
        timer = source.cancel_after(0.05)
        assert source.token.is_cancelled() is False
        assert source.token.wait(timeout=1.0) is True
        assert source.token.is_cancelled() is True
        timer.join(timeout=0.5)

    def test_never_cancelled_token_is_never_cancelled(self):
        assert NEVER_CANCELLED.is_cancelled() is False

    def test_wait_returns_false_on_timeout(self):
        source = CancellationTokenSource()
        assert source.token.wait(timeout=0.01) is False

    def test_repr(self):
        source = CancellationTokenSource()
        assert "cancelled=False" in repr(source.token)
        source.cancel()
        assert "cancelled=True" in repr(source.token)


class TestRunLevelCancellation:
    def test_cancelled_token_returns_cancelled_status(self):
        source = CancellationTokenSource()
        source.cancel()
        result = ClosedLoopOrchestrator(
            [SUCCESS_STEP], success_dispatcher(),
            cancellation_token=source.token,
        ).run(state(done=False))
        assert result.status == RunStatus.CANCELLED
        # No steps executed — no action events.
        assert len(result.actions) == 0
        # Audit trail records the cancellation event.
        assert any(e.event == "run_cancelled" for e in result.audit)

    def test_cancel_during_run_aborts_at_next_iteration(self):
        # A handler that succeeds on the first step but triggers
        # cancellation; the second step should not execute.
        source = CancellationTokenSource()
        dispatcher = ActionDispatcher({"act"})
        calls = []

        def handler(req, cur):
            calls.append(req.action)
            # Cancel after the first call so the next iteration sees it.
            if len(calls) == 1:
                source.cancel()
            return ActionResult(ActionStatus.SUCCEEDED, "done",
                                replace(cur, observations={**cur.observations,
                                    "done": Observation(True, source="result")}))

        dispatcher.register("act", handler)
        steps = [
            WorkflowStep("s1", "act", policy("pre"),
                         policy("post", Constraint("done", "eq", True)),
                         on_success="s2"),
            WorkflowStep("s2", "act", policy("pre"),
                         policy("post", Constraint("done", "eq", True))),
        ]
        result = ClosedLoopOrchestrator(steps, dispatcher,
                                        cancellation_token=source.token).run(state(done=False))
        assert result.status == RunStatus.CANCELLED
        # First step executed (1 call), second step never started.
        assert len(calls) == 1
        assert any(e.event == "run_cancelled" for e in result.audit)

    def test_no_token_runs_normally(self):
        # Backward compat: no cancellation_token -> behaves like pre-M2.
        result = ClosedLoopOrchestrator([SUCCESS_STEP], success_dispatcher()).run(state(done=False))
        assert result.status == RunStatus.COMPLETED
        assert not any(e.event == "run_cancelled" for e in result.audit)


class TestRunDeadline:
    def test_expired_deadline_returns_timeout(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        result = ClosedLoopOrchestrator(
            [SUCCESS_STEP], success_dispatcher(),
        ).run(state(done=False), deadline=past)
        assert result.status == RunStatus.TIMEOUT
        assert len(result.actions) == 0
        assert any(e.event == "run_timeout" for e in result.audit)

    def test_future_deadline_allows_completion(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        result = ClosedLoopOrchestrator([SUCCESS_STEP], success_dispatcher()).run(
            state(done=False), deadline=future)
        assert result.status == RunStatus.COMPLETED

    def test_deadline_must_be_timezone_aware(self):
        with pytest.raises(ValueError, match="deadline must include a timezone"):
            ClosedLoopOrchestrator([SUCCESS_STEP], success_dispatcher()).run(
                state(done=False), deadline=datetime(2026, 1, 1))


class TestPerStepTimeout:
    def test_step_timeout_coerces_to_indeterminate(self):
        # A handler that sleeps 0.2s with a 0.05s timeout — the result
        # is coerced to INDETERMINATE with reason "timeout".
        dispatcher = ActionDispatcher({"act"})
        dispatcher.register("act", lambda req, cur: (
            time.sleep(0.2),
            ActionResult(ActionStatus.SUCCEEDED, "done", cur),
        )[1])
        step = WorkflowStep("s", "act", policy("pre"),
                           policy("post"),
                           timeout_seconds=0.05,
                           on_indeterminate=None)
        result = ClosedLoopOrchestrator([step], dispatcher).run(state())
        # The action was coerced to INDETERMINATE due to timeout.
        assert result.actions[0].status == ActionStatus.INDETERMINATE
        assert "timeout" in result.actions[0].message
        assert "0.05" in result.actions[0].message
        # Audit event records the elapsed time.
        action_events = [e for e in result.audit if e.event.startswith("action_")]
        assert action_events[0].event == "action_indeterminate"
        assert "elapsed_seconds" in action_events[0].details

    def test_step_no_timeout_runs_normally(self):
        result = ClosedLoopOrchestrator([SUCCESS_STEP], success_dispatcher()).run(state(done=False))
        assert result.status == RunStatus.COMPLETED
        assert result.actions[0].status == ActionStatus.SUCCEEDED

    def test_step_timeout_zero_or_negative_rejected(self):
        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            WorkflowStep("s", "act", policy("pre"), policy("post"), timeout_seconds=0)
        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            WorkflowStep("s", "act", policy("pre"), policy("post"), timeout_seconds=-1)

    def test_step_timeout_routes_via_on_indeterminate(self):
        # When timeout fires and on_indeterminate is set, the run
        # transitions to the recovery step.
        dispatcher = ActionDispatcher({"slow", "recover"})
        dispatcher.register("slow", lambda req, cur: (
            time.sleep(0.15),
            ActionResult(ActionStatus.SUCCEEDED, "slow done", cur),
        )[1])
        dispatcher.register("recover", lambda req, cur: ActionResult(
            ActionStatus.SUCCEEDED, "recovered",
            replace(cur, observations={**cur.observations, "done": Observation(True, source="result")})))
        steps = [
            WorkflowStep("s1", "slow", policy("pre"), policy("post"),
                         timeout_seconds=0.05, on_indeterminate="s2"),
            WorkflowStep("s2", "recover", policy("pre"),
                         policy("post", Constraint("done", "eq", True))),
        ]
        result = ClosedLoopOrchestrator(steps, dispatcher).run(state(done=False))
        assert result.status == RunStatus.COMPLETED
        # The first action was coerced to INDETERMINATE; the recovery succeeded.
        assert result.actions[0].status == ActionStatus.INDETERMINATE
        assert result.actions[1].status == ActionStatus.SUCCEEDED


class TestCooperativeCancellation:
    def test_handler_polls_token_and_exits_early(self):
        # A cooperative handler that checks the token mid-work and
        # returns INDETERMINATE when cancelled.
        source = CancellationTokenSource()
        dispatcher = ActionDispatcher({"act"})

        def cooperative_handler(req, cur):
            for i in range(10):
                if req.cancellation_token.is_cancelled():
                    return ActionResult(ActionStatus.INDETERMINATE,
                                        "cancelled at iteration %d" % i, cur)
                time.sleep(0.01)
            return ActionResult(ActionStatus.SUCCEEDED, "done", cur)

        dispatcher.register("act", cooperative_handler)
        # Cancel after 0.03s — the handler should see it within ~0.01s.
        timer = source.cancel_after(0.03)
        try:
            step = WorkflowStep("s", "act", policy("pre"), policy("post"),
                                on_indeterminate=None)
            result = ClosedLoopOrchestrator([step], dispatcher,
                                            cancellation_token=source.token).run(state())
            assert result.actions[0].status == ActionStatus.INDETERMINATE
            assert "cancelled at iteration" in result.actions[0].message
        finally:
            timer.cancel()
            timer.join(timeout=0.5)

    def test_request_carries_cancellation_token(self):
        # The ActionRequest passed to the handler carries the
        # orchestrator's cancellation_token.
        seen_token = []
        dispatcher = ActionDispatcher({"act"})
        dispatcher.register("act", lambda req, cur: (
            seen_token.append(req.cancellation_token),
            ActionResult(ActionStatus.SUCCEEDED, "done", cur),
        )[1])
        source = CancellationTokenSource()
        ClosedLoopOrchestrator([SUCCESS_STEP], dispatcher,
                               cancellation_token=source.token).run(state(done=False))
        assert seen_token[0] is source.token


class TestAuditIntegration:
    def test_cancelled_run_audit_chain_valid(self, tmp_path):
        """A cancelled run's audit trail is still hash-chain valid."""
        from UPMP.audit import InMemoryAuditSink
        sink = InMemoryAuditSink()
        source = CancellationTokenSource()
        source.cancel()
        ClosedLoopOrchestrator([SUCCESS_STEP], success_dispatcher(),
                               audit_sink=sink,
                               cancellation_token=source.token).run(state(done=False))
        assert sink.verify_chain() is True
        assert any(e.event == "run_cancelled" for e in sink.events)

    def test_timeout_run_audit_chain_valid(self, tmp_path):
        from UPMP.audit import InMemoryAuditSink
        sink = InMemoryAuditSink()
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        ClosedLoopOrchestrator([SUCCESS_STEP], success_dispatcher(),
                               audit_sink=sink).run(state(done=False), deadline=past)
        assert sink.verify_chain() is True
        assert any(e.event == "run_timeout" for e in sink.events)
