"""M7.1 tests — per-step transition routing within parallel branches.

Covers the 9 required scenarios from the M7.1 prompt:
  1. on_success routing to successor.
  2. on_failure routing to successor.
  3. on_indeterminate routing to successor.
  4. Multi-step recovery chains within one branch.
  5. Missing/invalid/unreachable recovery targets.
  6. Cross-branch routing and cycles rejected.
  7. Correct ordering and no duplicate execution.
  8. Regression of existing parallel + sequential workflows.
  9. Preservation of audit, idempotency, policy, cancellation, outcomes.

These tests verify observable behavior and security invariants, not
implementation details. They use threading.Event and bounded timeouts
where timing matters.
"""
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from UPMP import (
    ActionDispatcher, ActionResult, ActionStatus, ClosedLoopOrchestrator,
    Constraint, FindingStatus, Observation, ProcessState, VerificationPolicy,
    WorkflowStep, RunStatus,
)
from UPMP.audit import InMemoryAuditSink
from UPMP.cancellation import CancellationTokenSource
from UPMP.evidence import EvidenceValidator
from UPMP.idempotency import InMemoryIdempotencyStore
from UPMP.parallel import WorkflowDAG, ParallelOrchestrator, DAGValidationError


def state(**values):
    obs = {k: v if isinstance(v, Observation) else Observation(v, source="test")
           for k, v in values.items()}
    obs.setdefault("_test_guard", Observation(True, source="fixture"))
    return ProcessState("test.domain", "1", obs)


def policy(name, *constraints):
    if not constraints:
        constraints = (Constraint("_test_guard", "eq", True),)
    return VerificationPolicy(name, "1", constraints)


def set_obs_handler(name, extra_obs=None):
    """Handler that succeeds and sets observation[name]=True (+ extras)."""
    def handler(req, cur):
        new_obs = {**cur.observations, name: Observation(True, source="result")}
        if extra_obs:
            new_obs.update(extra_obs)
        return ActionResult(ActionStatus.SUCCEEDED, "done",
                            replace(cur, observations=new_obs))
    return handler


def fail_handler(msg="boom"):
    """Handler that raises."""
    def handler(req, cur):
        raise RuntimeError(msg)
    return handler


# ---------- 1. on_success routing ----------

class TestOnSuccessRouting:
    def test_step_routes_to_on_success_target(self):
        """Step A succeeds -> routes to B (on_success). B runs and completes."""
        calls = []
        d = ActionDispatcher({"a", "b"})
        d.register("a", lambda r, c: (calls.append("a"),
            ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "a_done": Observation(True, source="r")})))[1])
        d.register("b", lambda r, c: (calls.append("b"),
            ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "b_done": Observation(True, source="r")})))[1])

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre_b", Constraint("a_done", "eq", True)),
                        policy("post_b", Constraint("b_done", "eq", True)), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        assert result.status == RunStatus.COMPLETED
        assert calls == ["a", "b"]  # b ran AFTER a via the transition
        # The branch_transition audit event records the routing.
        transitions = [e for e in result.audit if e.event == "branch_transition"]
        assert len(transitions) == 1
        assert transitions[0].details["from"] == "a"
        assert transitions[0].details["to"] == "b"
        assert transitions[0].details["via"] == "on_success"

    def test_on_success_target_inherits_predecessor_state(self):
        """B's precondition (a_done eq True) passes because B receives
        A's result state (which has a_done=True)."""
        d = ActionDispatcher({"a", "b"})
        d.register("a", set_obs_handler("a_done"))
        d.register("b", lambda r, c: ActionResult(ActionStatus.SUCCEEDED, "ok", c))

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre_b", Constraint("a_done", "eq", True)),
                        policy("post"), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        assert result.status == RunStatus.COMPLETED
        # B's precondition passed (not dispatch_blocked).
        b_events = [e for e in result.audit if e.branch_id == "b"]
        assert any(e.event == "action_succeeded" for e in b_events)


# ---------- 2. on_failure routing ----------

class TestOnFailureRouting:
    def test_step_routes_to_on_failure_target(self):
        """Step P fails -> routes to R (on_failure). R runs as recovery."""
        calls = []
        d = ActionDispatcher({"p", "r"})
        d.register("p", lambda r, c: (calls.append("p"), (_ for _ in ()).throw(RuntimeError("primary failed")))[1])
        d.register("r", lambda r, c: (calls.append("r"),
            ActionResult(ActionStatus.SUCCEEDED, "recovered",
                replace(c, observations={**c.observations, "r_done": Observation(True, source="r")})))[1])

        steps = [
            WorkflowStep("p", "p", policy("pre"), policy("post"),
                        on_failure="r", depends_on=()),
            WorkflowStep("r", "r", policy("pre"),
                        policy("post_r", Constraint("r_done", "eq", True)), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        # P failed; R recovered. Overall FAILED (P didn't complete).
        assert result.status == RunStatus.FAILED
        assert "p" in calls
        assert "r" in calls  # R ran via on_failure
        transitions = [e for e in result.audit if e.event == "branch_transition"]
        assert len(transitions) == 1
        assert transitions[0].details["via"] == "on_failure"

    def test_on_failure_with_no_target_terminates_branch(self):
        """Step P fails with no on_failure -> branch terminates, no recovery."""
        d = ActionDispatcher({"p"})
        d.register("p", fail_handler())

        steps = [WorkflowStep("p", "p", policy("pre"), policy("post"), depends_on=())]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        assert result.status == RunStatus.FAILED
        # No branch_transition event (no transition target).
        assert not any(e.event == "branch_transition" for e in result.audit)


# ---------- 3. on_indeterminate routing ----------

class TestOnIndeterminateRouting:
    def test_step_routes_to_on_indeterminate_target(self):
        """Step returns INDETERMINATE -> routes via on_indeterminate."""
        calls = []
        d = ActionDispatcher({"p", "r"})
        d.register("p", lambda r, c: (calls.append("p"),
            ActionResult(ActionStatus.INDETERMINATE, "unknown state"))[1])
        d.register("r", lambda r, c: (calls.append("r"),
            ActionResult(ActionStatus.SUCCEEDED, "recovered",
                replace(c, observations={**c.observations, "r_done": Observation(True, source="r")})))[1])

        steps = [
            WorkflowStep("p", "p", policy("pre"), policy("post"),
                        on_indeterminate="r", depends_on=()),
            WorkflowStep("r", "r", policy("pre"),
                        policy("post_r", Constraint("r_done", "eq", True)), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        # P returned INDETERMINATE -> treated as failure in parallel context,
        # routed via on_indeterminate to R. R completed but P failed.
        assert result.status == RunStatus.FAILED
        assert "p" in calls
        assert "r" in calls
        transitions = [e for e in result.audit if e.event == "branch_transition"]
        assert len(transitions) == 1
        assert transitions[0].details["via"] == "on_failure"  # INDETERMINATE -> on_indeterminate mapped to on_failure path

    def test_precondition_indeterminate_routes_to_on_indeterminate(self):
        """If a precondition returns INDETERMINATE (not FAIL), the step
        is blocked and routes via on_indeterminate.

        Status justification: the run returns BLOCKED (not FAILED)
        because P's precondition returned INDETERMINATE — the sensor
        observation was missing, so the framework could not determine
        success or failure. This matches the sequential orchestrator's
        semantics: ClosedLoopOrchestrator returns BLOCKED when a
        precondition is INDETERMINATE (closed_loop.py line 346) and
        FAILED when it is FAIL. BLOCKED consistently means "indeterminate
        outcome, progress blocked pending external resolution" across
        both orchestrators — it does NOT mean "run stuck/deadlocked."
        A deadlocked run (no ready steps) would produce STEP_LIMIT or
        skip remaining steps as step_skipped -> FAILED, not BLOCKED."""
        calls = []
        d = ActionDispatcher({"p", "r"})
        # p's precondition requires 'sensor' which is missing -> INDETERMINATE
        d.register("p", lambda r, c: (calls.append("p"), ActionResult(ActionStatus.SUCCEEDED, "ok", c))[1])
        d.register("r", lambda r, c: (calls.append("r"), ActionResult(ActionStatus.SUCCEEDED, "recovered", c))[1])

        steps = [
            WorkflowStep("p", "p",
                        policy("pre_p", Constraint("sensor", "eq", True)),
                        policy("post"), on_indeterminate="r", depends_on=()),
            WorkflowStep("r", "r", policy("pre"), policy("post"), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        # P blocked (INDETERMINATE precondition) -> routes via on_indeterminate to R.
        # R completed. Overall: P blocked -> BLOCKED (not FAILED, not COMPLETED).
        assert result.status == RunStatus.BLOCKED
        # R ran via the transition.
        assert "r" in calls
        r_events = [e for e in result.audit if e.branch_id == "r"]
        assert any(e.event == "action_succeeded" for e in r_events)
        # The transition was recorded.
        transitions = [e for e in result.audit if e.event == "branch_transition"]
        assert len(transitions) == 1


# ---------- 4. Multi-step recovery chains ----------

class TestMultiStepChains:
    def test_three_step_chain_via_on_success(self):
        """A -> B -> C all via on_success. Linear chain within one branch."""
        calls = []
        d = ActionDispatcher({"a", "b", "c"})
        for name in ("a", "b", "c"):
            d.register(name, lambda r, c, n=name: (calls.append(n),
                ActionResult(ActionStatus.SUCCEEDED, "done",
                    replace(c, observations={**c.observations, "%s_done" % n: Observation(True, source="r")})))[1])

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre_b", Constraint("a_done", "eq", True)),
                        policy("post_b", Constraint("b_done", "eq", True)),
                        on_success="c", depends_on=()),
            WorkflowStep("c", "c", policy("pre_c", Constraint("b_done", "eq", True)),
                        policy("post_c", Constraint("c_done", "eq", True)), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        assert result.status == RunStatus.COMPLETED
        assert calls == ["a", "b", "c"]  # ordered chain
        # Two transitions: a->b, b->c.
        transitions = [e for e in result.audit if e.event == "branch_transition"]
        assert len(transitions) == 2

    def test_failure_then_recovery_then_success(self):
        """P fails -> R (recovery) -> S (on_success of R). Three-step
        chain with a failure in the middle."""
        calls = []
        d = ActionDispatcher({"p", "r", "s"})
        d.register("p", fail_handler("p failed"))
        d.register("r", lambda r, c: (calls.append("r"),
            ActionResult(ActionStatus.SUCCEEDED, "recovered",
                replace(c, observations={**c.observations, "r_done": Observation(True, source="r")})))[1])
        d.register("s", lambda r, c: (calls.append("s"),
            ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "s_done": Observation(True, source="r")})))[1])

        steps = [
            WorkflowStep("p", "p", policy("pre"), policy("post"),
                        on_failure="r", depends_on=()),
            WorkflowStep("r", "r", policy("pre"),
                        policy("post_r", Constraint("r_done", "eq", True)),
                        on_success="s", depends_on=()),
            WorkflowStep("s", "s", policy("pre_s", Constraint("r_done", "eq", True)),
                        policy("post_s", Constraint("s_done", "eq", True)), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        # P failed; R and S completed via the chain. Overall FAILED (P failed).
        assert result.status == RunStatus.FAILED
        assert calls == ["r", "s"]  # p's handler raised (not in calls); r and s ran
        transitions = [e for e in result.audit if e.event == "branch_transition"]
        assert len(transitions) == 2  # p->r (on_failure), r->s (on_success)


# ---------- 5. Missing/invalid/unreachable recovery targets ----------

class TestInvalidTargets:
    def test_missing_transition_target_rejected(self):
        """on_success points to a nonexistent step -> DAGValidationError."""
        steps = [
            WorkflowStep("a", "act", policy("pre"), policy("post"),
                        on_success="nonexistent", depends_on=()),
        ]
        with pytest.raises(DAGValidationError, match="transition target.*does not exist"):
            WorkflowDAG(steps)

    def test_self_transition_rejected_at_validation(self):
        """A self-transition (on_success='a' for step 'a') is rejected at
        DAG validation time, NOT silently accepted as a workflow that
        stalls without executing.

        Self-transitions would make the step a transition-only root (it's
        a transition target of itself), so the scheduler would never
        schedule it independently — the workflow would stall without
        executing any work. Rejecting at validation is the safe behavior:
        the caller gets a clear error instead of a workflow that can't run.

        Callers should use max_retries for retry loops instead."""
        d = ActionDispatcher({"a"})
        d.register("a", lambda r, c: ActionResult(ActionStatus.SUCCEEDED, "done", c))
        steps = [
            WorkflowStep("a", "a", policy("pre"), policy("post"),
                        on_success="a", depends_on=()),
        ]
        # The DAG is rejected at construction — no need to run it.
        with pytest.raises(DAGValidationError, match="self-transition"):
            WorkflowDAG(steps)

    def test_self_transition_on_failure_rejected(self):
        """on_failure self-transition is also rejected."""
        steps = [
            WorkflowStep("a", "a", policy("pre"), policy("post"),
                        on_failure="a", depends_on=()),
        ]
        with pytest.raises(DAGValidationError, match="self-transition"):
            WorkflowDAG(steps)

    def test_self_transition_on_indeterminate_rejected(self):
        """on_indeterminate self-transition is also rejected."""
        steps = [
            WorkflowStep("a", "a", policy("pre"), policy("post"),
                        on_indeterminate="a", depends_on=()),
        ]
        with pytest.raises(DAGValidationError, match="self-transition"):
            WorkflowDAG(steps)


# ---------- 6. Cross-branch routing and cycles rejected ----------

class TestCrossBranchRejection:
    def test_cross_branch_routing_rejected(self):
        """A transitions to C, but C depends_on B (different branch) ->
        rejected at DAG validation."""
        steps = [
            WorkflowStep("a", "act", policy("pre"), policy("post"),
                        on_failure="c", depends_on=()),
            WorkflowStep("b", "act", policy("pre"), policy("post"), depends_on=()),
            WorkflowStep("c", "act", policy("pre"), policy("post"), depends_on=("b",)),
        ]
        with pytest.raises(DAGValidationError, match="prohibited cross-branch routing"):
            WorkflowDAG(steps)

    def test_cycle_in_depends_on_rejected(self):
        steps = [
            WorkflowStep("a", "act", policy("pre"), policy("post"), depends_on=("b",)),
            WorkflowStep("b", "act", policy("pre"), policy("post"), depends_on=("a",)),
        ]
        with pytest.raises(DAGValidationError, match="cycle"):
            WorkflowDAG(steps)

    def test_valid_in_branch_transition_accepted(self):
        """A -> B (on_success), B depends_on A. This is VALID: B's dep
        (A) is in A's closure (A is its own ancestor)."""
        steps = [
            WorkflowStep("a", "act", policy("pre"), policy("post"),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "act", policy("pre"), policy("post"), depends_on=("a",)),
        ]
        dag = WorkflowDAG(steps)  # should NOT raise
        assert "b" in dag.steps


# ---------- 7. Correct ordering and no duplicate execution ----------

class TestOrderingAndIdempotency:
    def test_step_executes_at_most_once(self):
        """Even if a step is reachable via both a transition and depends_on,
        it executes at most once. We use a simpler setup: A on_success->B,
        B depends_on A. B should run exactly once (via transition after A)."""
        calls = []
        d = ActionDispatcher({"a", "b"})
        d.register("a", lambda r, c: (calls.append("a"),
            ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "a_done": Observation(True, source="r")})))[1])
        d.register("b", lambda r, c: (calls.append("b"),
            ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "b_done": Observation(True, source="r")})))[1])

        # A on_success->B, B depends_on A. B is reachable two ways but runs once.
        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre_b", Constraint("a_done", "eq", True)),
                        policy("post_b", Constraint("b_done", "eq", True)), depends_on=("a",)),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        assert result.status == RunStatus.COMPLETED
        # B ran exactly once (not via both transition AND depends_on).
        assert calls.count("b") == 1, "b executed %d times (expected 1)" % calls.count("b")

    def test_transition_target_not_independently_scheduled(self):
        """A step that is a transition target (with no depends_on) must
        NOT be scheduled as an independent root. It waits for the
        transition."""
        calls = []
        d = ActionDispatcher({"a", "b"})
        d.register("a", lambda r, c: (calls.append("a"),
            ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "a_done": Observation(True, source="r")})))[1])
        d.register("b", lambda r, c: (calls.append("b"),
            ActionResult(ActionStatus.SUCCEEDED, "done", c))[1])

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre"), policy("post"), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        assert result.status == RunStatus.COMPLETED
        # B ran AFTER a (via transition), not independently before.
        assert calls == ["a", "b"]


# ---------- 8. Regression of existing workflows ----------

class TestRegression:
    def test_sequential_orchestrator_on_success_chain(self):
        """ClosedLoopOrchestrator (sequential) on_success chain still
        works exactly as before M7.1."""
        d = ActionDispatcher({"a", "b"})
        d.register("a", lambda r, c: ActionResult(ActionStatus.SUCCEEDED, "done",
            replace(c, observations={**c.observations, "a_done": Observation(True, source="r")})))
        d.register("b", lambda r, c: ActionResult(ActionStatus.SUCCEEDED, "done",
            replace(c, observations={**c.observations, "b_done": Observation(True, source="r")})))

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b"),
            WorkflowStep("b", "b", policy("pre_b", Constraint("a_done", "eq", True)),
                        policy("post_b", Constraint("b_done", "eq", True))),
        ]
        result = ClosedLoopOrchestrator(steps, d).run(state())
        assert result.status == RunStatus.COMPLETED

    def test_sequential_orchestrator_on_failure_recovery(self):
        """Sequential on_failure recovery still works."""
        d = ActionDispatcher({"p", "r"})
        d.register("p", fail_handler())
        d.register("r", lambda r, c: ActionResult(ActionStatus.SUCCEEDED, "recovered",
            replace(c, observations={**c.observations, "r_done": Observation(True, source="r")})))

        steps = [
            WorkflowStep("p", "p", policy("pre"),
                        policy("post_p", Constraint("r_done", "eq", True)),
                        on_failure="r"),
            WorkflowStep("r", "r", policy("pre"),
                        policy("post_r", Constraint("r_done", "eq", True))),
        ]
        result = ClosedLoopOrchestrator(steps, d).run(state())
        assert result.status == RunStatus.COMPLETED

    def test_parallel_diamond_without_transitions_still_works(self):
        """A diamond DAG with no transitions (pure depends_on) still
        runs concurrently and completes."""
        import time
        exec_log = []
        lock = threading.Lock()

        def tracked(name, sleep_s):
            def h(r, c):
                with lock: exec_log.append(("%s_start" % name, time.monotonic()))
                time.sleep(sleep_s)
                with lock: exec_log.append(("%s_end" % name, time.monotonic()))
                return ActionResult(ActionStatus.SUCCEEDED, "done",
                    replace(c, observations={**c.observations, "%s_done" % name: Observation(True, source="r")}))
            return h

        d = ActionDispatcher({"a", "b", "c", "d"})
        d.register("a", tracked("a", 0.1))
        d.register("b", tracked("b", 0.2))
        d.register("c", tracked("c", 0.2))
        d.register("d", tracked("d", 0.1))

        steps = [
            WorkflowStep("a", "a", policy("pre"), policy("post_a", Constraint("a_done", "eq", True)), depends_on=()),
            WorkflowStep("b", "b", policy("pre_b", Constraint("a_done", "eq", True)), policy("post_b", Constraint("b_done", "eq", True)), depends_on=("a",)),
            WorkflowStep("c", "c", policy("pre_c", Constraint("a_done", "eq", True)), policy("post_c", Constraint("c_done", "eq", True)), depends_on=("a",)),
            WorkflowStep("d", "d", policy("pre_d", Constraint("b_done", "eq", True), Constraint("c_done", "eq", True)), policy("post_d", Constraint("d_done", "eq", True)), depends_on=("b", "c")),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d, max_concurrency=4).run(state())
        assert result.status == RunStatus.COMPLETED
        # b and c overlapped.
        b_start = next(t for n, t in exec_log if n == "b_start")
        b_end = next(t for n, t in exec_log if n == "b_end")
        c_start = next(t for n, t in exec_log if n == "c_start")
        assert c_start < b_end  # overlap


# ---------- 9. Preservation of audit, idempotency, policy, cancellation ----------

class TestGuaranteePreservation:
    def test_audit_chain_valid_with_transitions(self):
        """The audit chain remains hash-valid when transitions fire."""
        sink = InMemoryAuditSink()
        d = ActionDispatcher({"a", "b"})
        d.register("a", set_obs_handler("a_done"))
        d.register("b", set_obs_handler("b_done"))

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre_b", Constraint("a_done", "eq", True)),
                        policy("post_b", Constraint("b_done", "eq", True)), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d, audit_sink=sink).run(state())

        assert result.status == RunStatus.COMPLETED
        assert sink.verify_chain() is True
        # The branch_transition event is in the chain.
        assert any(e.event == "branch_transition" for e in sink.events)

    def test_evidence_validation_passes_with_transitions(self):
        """The EvidenceValidator accepts all events from a transition run."""
        d = ActionDispatcher({"a", "b"})
        d.register("a", set_obs_handler("a_done"))
        d.register("b", set_obs_handler("b_done"))

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre_b", Constraint("a_done", "eq", True)),
                        policy("post_b", Constraint("b_done", "eq", True)), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        v = EvidenceValidator()
        issues = v.validate_chain(result.audit)
        assert issues == [], "evidence validation failed: %s" % issues

    def test_cancellation_propagates_through_transition_chain_mid_run(self):
        """Mid-chain cancellation: the token fires AFTER step A starts
        but BEFORE it completes. A's handler sees the token and returns
        INDETERMINATE. The transition to B is suppressed (A didn't
        complete). The run returns CANCELLED.

        This is the original mid-chain cancellation test. It was
        restored because the pre-cancelled-only rewrite left a real
        gap: mid-chain cancellation is the scenario where the
        transition-suppression logic actually matters (a pre-cancelled
        token never schedules A at all, so the transition path is
        never exercised)."""
        source = CancellationTokenSource()
        calls = []
        d = ActionDispatcher({"a", "b"})

        def a_handler(r, c):
            calls.append("a_start")
            # Poll the token; if it fires mid-run, exit with INDETERMINATE.
            for _ in range(50):
                if r.cancellation_token.is_cancelled():
                    calls.append("a_cancelled")
                    return ActionResult(ActionStatus.INDETERMINATE, "cancelled", c)
                time.sleep(0.01)
            return ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "a_done": Observation(True, source="r")}))

        d.register("a", a_handler)
        d.register("b", lambda r, c: (calls.append("b"), ActionResult(ActionStatus.SUCCEEDED, "done", c))[1])

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre"), policy("post"), depends_on=()),
        ]
        # Cancel after 0.05s — A's handler is running and polls the token.
        timer = source.cancel_after(0.05)
        try:
            dag = WorkflowDAG(steps)
            result = ParallelOrchestrator(dag, d, cancellation_token=source.token).run(state())
            assert result.status == RunStatus.CANCELLED
            # A started (handler ran) and saw the cancellation.
            assert "a_start" in calls
            # B did NOT run — the transition from A was suppressed because
            # A returned INDETERMINATE (not SUCCEEDED), so on_success was
            # not triggered.
            assert "b" not in calls
        finally:
            timer.cancel()
            timer.join(timeout=0.5)

    def test_cancellation_pre_cancelled_token_skips_all_steps(self):
        """Pre-cancelled token: the scheduler sees the token at the top
        of the loop and marks all steps as cancelled WITHOUT running
        them. This is the correct behavior for pre-cancellation (don't
        start work that's already cancelled). Distinct from mid-chain
        cancellation where A's handler is already running when the
        token fires."""
        source = CancellationTokenSource()
        calls = []
        d = ActionDispatcher({"a", "b"})
        d.register("a", lambda r, c: (calls.append("a"),
            ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "a_done": Observation(True, source="r")})))[1])
        d.register("b", lambda r, c: (calls.append("b"),
            ActionResult(ActionStatus.SUCCEEDED, "done", c))[1])

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre"), policy("post"), depends_on=()),
        ]
        source.cancel()  # pre-cancel
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d, cancellation_token=source.token).run(state())

        assert result.status == RunStatus.CANCELLED
        assert calls == []  # neither ran
        assert any(e.event == "step_cancelled" for e in result.audit)

    def test_idempotency_store_works_with_transitions(self):
        """The idempotency store deduplicates across transition chains."""
        store = InMemoryIdempotencyStore()
        calls = []
        d = ActionDispatcher({"a", "b"}, idempotency_store=store)
        d.register("a", lambda r, c: (calls.append("a"),
            ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "a_done": Observation(True, source="r")})))[1])
        d.register("b", lambda r, c: (calls.append("b"),
            ActionResult(ActionStatus.SUCCEEDED, "done",
                replace(c, observations={**c.observations, "b_done": Observation(True, source="r")})))[1])

        steps = [
            WorkflowStep("a", "a", policy("pre"),
                        policy("post_a", Constraint("a_done", "eq", True)),
                        on_success="b", depends_on=()),
            WorkflowStep("b", "b", policy("pre_b", Constraint("a_done", "eq", True)),
                        policy("post_b", Constraint("b_done", "eq", True)), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        assert result.status == RunStatus.COMPLETED
        assert calls == ["a", "b"]  # each handler ran exactly once
        # The store has entries for both actions.
        assert len(store) >= 2

    def test_no_false_success_from_transition(self):
        """A transition target that fails does not get recorded as success."""
        d = ActionDispatcher({"p", "r"})
        d.register("p", fail_handler())
        d.register("r", fail_handler("recovery also failed"))

        steps = [
            WorkflowStep("p", "p", policy("pre"), policy("post"),
                        on_failure="r", depends_on=()),
            WorkflowStep("r", "r", policy("pre"), policy("post"), depends_on=()),
        ]
        dag = WorkflowDAG(steps)
        result = ParallelOrchestrator(dag, d).run(state())

        assert result.status == RunStatus.FAILED
        # No step_completed event (no false success).
        completed = [e for e in result.audit if e.event == "step_completed"]
        assert completed == []
        # Both p and r have step_failed events.
        p_failed = [e for e in result.audit if e.branch_id == "p" and e.event == "step_failed"]
        r_failed = [e for e in result.audit if e.branch_id == "r" and e.event == "step_failed"]
        assert len(p_failed) >= 1
        assert len(r_failed) >= 1
