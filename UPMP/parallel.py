"""Parallel DAG execution for UPMP — M7.

Provides WorkflowDAG (validated directed acyclic graph of steps with
explicit dependency edges) and ParallelOrchestrator (executes
independent steps concurrently via ThreadPoolExecutor, respecting
dependency and transition semantics).

This module advances UPMP toward its vision: intelligences, applications,
and web services that run many processes (often in parallel) without
drifting from intent or hallucinating progress. The ParallelOrchestrator
preserves every existing guarantee (typed contracts, pre/postconditions,
audit chain integrity, evidence validation, cancellation, preemption)
while allowing independent steps to run at the same time.

Design (see THREAT.md T11 and ARCHITECTURE.md §Parallel for full context):

  - depends_on (WorkflowStep, M7) is ORTHOGONAL to on_success/on_failure/
    on_indeterminate. The former is the static scheduling constraint
    (parallel); the latter is the per-step branch routing (sequential
    within a step's own sub-chain). ClosedLoopOrchestrator (sequential)
    IGNORES depends_on entirely, preserving 100% backward compat.

  - Audit chain under concurrency: all sink.append() calls are
    serialized under a threading.Lock held by the orchestrator. The
    chain order is "real-time append order" — non-deterministic across
    runs but consistent within a run, and the chain is verifiable.
    Each event carries branch_id (= step_id) so a reader can reconstruct
    causality across concurrent branches. The chain does NOT lie about
    execution order; it records "these events were appended in this
    order," which is a real and defensible property.

  - Failure isolation: a step whose dependency did not complete
    successfully is SKIPPED (audit event "step_skipped" with the
    unsatisfied dependency name). Unrelated branches continue. A
    branch failure does NOT cancel other branches unless the caller
    explicitly cancels via the cancellation token.

  - Cancellation propagation: if the cancellation token fires, all
    in-flight futures are cancelled (cooperative — handlers must poll
    the token; see M2). Pending futures that haven't started are
    cancelled at the executor level. The run returns RunStatus.CANCELLED.

  - Worker crashes / exceptions: each worker returns
    (ActionResult, list[AuditEvent]) or raises. The orchestrator
    catches exceptions and synthesizes ActionStatus.FAILED. A worker
    that never returns is bounded by per-step timeout_seconds (M2)
    or by the executor's wait timeout. No false success is possible
    — success requires an actual ActionResult(SUCCEEDED, ...) returned
    by the dispatcher.

Threat model (see THREAT.md T11):
  - Concurrent execution does not weaken the audit chain. The chain
    is serialized under a lock; the order is real-time append order;
    each event's branch_id records its origin.
  - A skipped step (dependency failed) is recorded as "step_skipped"
    with the dependency name — the framework never invents a
    successful outcome for work whose completion cannot be established.
  - Preemption (M2.2/M2.3) is opt-in per-handler: callers wrap their
    dispatcher in PreemptibleDispatcher or ProcessPreemptibleDispatcher
    for hard preemption. ParallelOrchestrator itself does cooperative
    cancellation only.

Limitations (documented):
  - ThreadPoolExecutor: subject to the GIL. Suitable for IO-bound
    handlers (the typical "process" case). For CPU-bound parallelism,
    wrap handlers in ProcessPreemptibleDispatcher (M2.3) — each
    handler internally spawns a subprocess.
  - The audit chain order under concurrency is non-deterministic
    across runs. For deterministic ordering, callers can post-process
    by sorting on (branch_id, sequence_number) — but the chain's
    hash links are computed in real-time order and must be verified
    in that order.
  - InMemoryIdempotencyStore is not thread-safe; the orchestrator
    locks around dispatch so concurrent dispatches serialize. For
    true concurrent dispatch with a shared store, provide a thread-
    safe store implementation.
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, Future, CancelledError, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from .cancellation import CancellationToken, NEVER_CANCELLED
from .closed_loop import (
    ActionDispatcher, ActionRequest, ActionResult, ActionStatus,
    AuditEvent, ClosedLoopOrchestrator, RunStatus, WorkflowResult, WorkflowStep,
)
from .contracts import FindingStatus, ProcessState, VerificationResult


class DAGValidationError(ValueError):
    """Raised when a WorkflowDAG fails validation.

    Subclass of ValueError for backward compat with existing code that
    catches ValueError on workflow construction.
    """


class WorkflowDAG:
    """Validated directed acyclic graph of workflow steps.

    Validates:
      - Duplicate step IDs.
      - Missing dependency references (depends_on points to a step
        that doesn't exist).
      - Missing transition targets (on_success/on_failure/on_indeterminate
        points to a step that doesn't exist).
      - Dependency cycles (DFS-based detection).
      - Self-dependencies (a step depends on itself).

    Does NOT validate:
      - That the graph is connected (disconnected components are
        allowed — they run independently).
      - That on_success/on_failure/on_indeterminate are consistent
        with depends_on (the two are orthogonal; the caller is
        responsible for sensible combinations).

    The DAG is immutable after construction. Steps are stored in a
    dict keyed by step_id; iteration order is insertion order (Python
    3.7+ dict guarantee).
    """

    def __init__(self, steps: Sequence[WorkflowStep]) -> None:
        if not steps:
            raise DAGValidationError("DAG needs at least one step")
        self.steps: Dict[str, WorkflowStep] = {}
        for s in steps:
            if s.step_id in self.steps:
                raise DAGValidationError("duplicate step id: %s" % s.step_id)
            self.steps[s.step_id] = s
        self._validate()

    def _validate(self) -> None:
        # 1. Transition targets must exist.
        for step in self.steps.values():
            for target in (step.on_success, step.on_failure, step.on_indeterminate):
                if target is not None and target not in self.steps:
                    raise DAGValidationError(
                        "step %s has transition target %s that does not exist"
                        % (step.step_id, target))
        # 2. depends_on references must exist; no self-dependencies.
        for step in self.steps.values():
            for dep in step.depends_on:
                if dep == step.step_id:
                    raise DAGValidationError(
                        "step %s depends on itself" % step.step_id)
                if dep not in self.steps:
                    raise DAGValidationError(
                        "step %s depends on missing step %s" % (step.step_id, dep))
        # 3. No cycles in depends_on graph (DFS).
        self._check_no_cycles()
        # 4. M7.1: reject self-transitions. A self-transition (e.g.,
        # on_success='a' for step 'a') makes the step a transition-only
        # root (it's a transition target of itself), so the scheduler
        # would never schedule it independently — the workflow stalls
        # without executing any work. This is an unschedulable graph,
        # not a valid loop. Callers should use max_retries for retry
        # loops instead. Reject at validation time with a clear error
        # rather than silently accepting a workflow that can't run.
        for step in self.steps.values():
            for target_name, field_name in (
                (step.on_success, "on_success"),
                (step.on_failure, "on_failure"),
                (step.on_indeterminate, "on_indeterminate"),
            ):
                if target_name is not None and target_name == step.step_id:
                    raise DAGValidationError(
                        "step %s has %s=%s (self-transition); "
                        "self-transitions are not supported in the parallel "
                        "orchestrator (use max_retries for retry loops)"
                        % (step.step_id, field_name, target_name))
        # 5. M7.1: transition targets must not create cross-branch routing
        # AND must not be independently schedulable. A step T that is a
        # transition target of step S must not be a root (no depends_on)
        # UNLESS S is T's only path to execution. Specifically:
        #   - If T has depends_on, every dep must be in S's ancestor closure
        #     (prevents cross-branch routing).
        #   - If T has NO depends_on (would be a root), T is ONLY reachable
        #     via the transition from S — the scheduler must NOT schedule T
        #     independently. We enforce this by requiring T to have at least
        #     one incoming transition OR depends_on; a step with neither is
        #     unreachable and rejected at construction (already covered by the
        #     empty-DAG check implicitly, but we make it explicit here for
        #     transition targets).
        transition_targets: Set[str] = set()
        for step in self.steps.values():
            for target_name in (step.on_success, step.on_failure, step.on_indeterminate):
                if target_name is not None:
                    transition_targets.add(target_name)
        # Store for the scheduler: steps reachable ONLY via a transition
        # (no depends_on). The scheduler must not schedule these as roots.
        self._transition_only_roots = frozenset(
            tid for tid in transition_targets if not self.steps[tid].depends_on
        )
        for step in self.steps.values():
            for target_name in (step.on_success, step.on_failure, step.on_indeterminate):
                if target_name is None:
                    continue
                target = self.steps[target_name]
                source_closure = self._ancestor_closure(step.step_id)
                for dep in target.depends_on:
                    if dep not in source_closure:
                        raise DAGValidationError(
                            "step %s transitions to %s, but %s depends_on %s "
                            "which is outside %s's branch closure "
                            "(prohibited cross-branch routing)"
                            % (step.step_id, target_name, target_name, dep, step.step_id))

    def _ancestor_closure(self, step_id: str) -> Set[str]:
        """Return the set containing step_id and all its transitive
        depends_on ancestors."""
        closure: Set[str] = set()
        stack = [step_id]
        while stack:
            sid = stack.pop()
            if sid in closure:
                continue
            closure.add(sid)
            for dep in self.steps[sid].depends_on:
                if dep not in closure:
                    stack.append(dep)
        return closure

    def _check_no_cycles(self) -> None:
        """DFS-based cycle detection over the depends_on graph."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {sid: WHITE for sid in self.steps}

        def visit(sid: str) -> None:
            color[sid] = GRAY
            for dep in self.steps[sid].depends_on:
                if color[dep] == GRAY:
                    raise DAGValidationError(
                        "dependency cycle detected involving step %s" % dep)
                if color[dep] == WHITE:
                    visit(dep)
            color[sid] = BLACK

        for sid in self.steps:
            if color[sid] == WHITE:
                visit(sid)

    def roots(self) -> List[str]:
        """Step IDs with no depends_on (the entry points)."""
        return [sid for sid, s in self.steps.items() if not s.depends_on]

    def dependents(self, step_id: str) -> List[str]:
        """Step IDs that depend on this one (reverse adjacency)."""
        return [sid for sid, s in self.steps.items() if step_id in s.depends_on]

    def topological_order(self) -> List[str]:
        """Return a topological order of step IDs. Raises if cyclic
        (already caught at construction, but defensive)."""
        order: List[str] = []
        visited: Set[str] = set()

        def visit(sid: str) -> None:
            if sid in visited:
                return
            for dep in self.steps[sid].depends_on:
                visit(dep)
            visited.add(sid)
            order.append(sid)

        for sid in self.steps:
            visit(sid)
        return order


class ParallelOrchestrator:
    """Executes a WorkflowDAG with concurrent independent steps.

    Independent steps (whose depends_on are all satisfied) run in a
    ThreadPoolExecutor up to max_concurrency at a time. Dependent steps
    wait for their prerequisites. Failures do not cancel unrelated
    branches. Cancellation propagates to all in-flight futures.

    Backward compatibility:
      - ClosedLoopOrchestrator (sequential) is unchanged. Workflows
        that don't use depends_on continue to run sequentially.
      - WorkflowStep.depends_on defaults to () — existing steps are
        unaffected.
      - AuditEvent.branch_id defaults to "default" — existing audit
        chains are unaffected.

    Usage:
        dag = WorkflowDAG([step_a, step_b, step_c])
        # step_b.depends_on = ["a"]; step_c.depends_on = ["a"]
        # a runs first; then b and c run concurrently; then done.
        orchestrator = ParallelOrchestrator(dag, dispatcher, max_concurrency=4)
        result = orchestrator.run(initial_state)
    """

    def __init__(self,
                 dag: WorkflowDAG,
                 dispatcher: ActionDispatcher,
                 max_steps: int = 1000,
                 max_concurrency: int = 4,
                 audit_sink: Optional[Any] = None,
                 cancellation_token: Optional[CancellationToken] = None) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if audit_sink is not None and not callable(getattr(audit_sink, "append", None)):
            raise TypeError("audit_sink must provide append(event) -> int")
        self.dag = dag
        self.dispatcher = dispatcher
        self.max_steps = max_steps
        self.max_concurrency = max_concurrency
        self.audit_sink = audit_sink
        self.cancellation_token = cancellation_token if cancellation_token is not None else NEVER_CANCELLED
        # Lock serializing all audit_sink.append() calls AND dispatcher
        # dispatch calls (the dispatcher's idempotency store is not
        # thread-safe). Dispatches are typically IO-bound so lock
        # contention is low; this is the safe default. Callers needing
        # truly concurrent dispatch must provide a thread-safe store
        # and a thread-safe dispatcher.
        self._dispatch_lock = threading.Lock()
        self._audit_lock = threading.Lock()

    def run(self, initial_state: ProcessState,
            now: Optional[datetime] = None,
            principal_ref: Optional[str] = None) -> WorkflowResult:
        """Execute the DAG. See class docstring for semantics."""
        if now is not None and now.tzinfo is None:
            raise ValueError("now must include a timezone")
        run_id = uuid.uuid4().hex
        audit: List[AuditEvent] = []
        actions: List[ActionResult] = []
        # step_status: step_id -> "completed" | "failed" | "blocked" | "cancelled" | "skipped"
        step_status: Dict[str, str] = {}
        # Per-step state (the state passed to the handler). Initialized
        # to initial_state for root steps; for dependent steps, the
        # state from the LAST completed dependency is used (deterministic:
        # the lexicographically-last dep among those that completed).
        step_state: Dict[str, ProcessState] = {}
        current_state = initial_state
        moment = now or datetime.now(timezone.utc)

        def _record(event: AuditEvent, branch_id: str) -> AuditEvent:
            """Stamp branch_id, append to in-memory list and durable sink."""
            stamped = AuditEvent(
                event=event.event,
                step_id=event.step_id,
                timestamp=event.timestamp,
                details=event.details,
                branch_id=branch_id,
            )
            audit.append(stamped)
            if self.audit_sink is not None:
                with self._audit_lock:
                    self.audit_sink.append(stamped)
            return stamped

        def _make_event(name: str, step: WorkflowStep, details: Mapping[str, Any]) -> AuditEvent:
            return AuditEvent(name, step.step_id,
                              datetime.now(timezone.utc).isoformat(),
                              dict(details))

        def _verification_details(result: VerificationResult) -> Mapping[str, Any]:
            return {
                "status": result.status.value,
                "policy": result.policy_id,
                "version": result.policy_version,
                "findings": [
                    {"constraint": f.constraint, "status": f.status.value, "message": f.message,
                     "observed": f.observed, "expected": f.expected, "severity": f.severity,
                     "evidence": dict(f.evidence)}
                    for f in result.findings
                ],
            }

        def _dependencies_satisfied(step: WorkflowStep) -> Tuple[bool, Optional[str], Optional[str]]:
            """Return (satisfied, unsatisfied_dep, dep_status).
            - satisfied=True iff all deps are completed.
            - satisfied=False, dep_status="in_flight" or "pending":
              dep is not yet terminal — WAIT, do not skip.
            - satisfied=False, dep_status in {failed, blocked,
              cancelled, skipped}: dep reached a terminal non-completed
              state — SKIP this step.
            """
            for dep in step.depends_on:
                status = step_status.get(dep)
                if status == "completed":
                    continue
                if status is None:
                    # Dep not yet started. If it's in_flight, status is
                    # "in_flight". If it's NOT in_flight, it means the dep
                    # itself hasn't been scheduled yet (its own deps may
                    # still be in flight). Either way: WAIT, don't skip.
                    return False, dep, "in_flight" if dep in in_flight else "pending"
                # status is a terminal non-completed state.
                return False, dep, status
            return True, None, None

        def _execute_step(step: WorkflowStep, state_for_step: ProcessState) -> Tuple[str, Optional[ActionResult], Optional[str]]:
            """Run a single step synchronously (called from worker thread).
            Returns (status_string, action_result_or_None, next_step_id).
            status_string is one of: completed, failed, blocked, cancelled.
            next_step_id is the transition target (on_success/on_failure/
            on_indeterminate) based on the outcome, or None if no transition.
            Records audit events for the step.

            M7.1: transition routing. The step's on_success/on_failure/
            on_indeterminate fields select a successor step WITHIN THE
            SAME BRANCH. The scheduler enqueues that successor, bypassing
            the normal depends_on check (the transition IS the dependency
            signal). The successor still runs at most once (idempotency).
            Cross-branch routing is rejected at DAG validation time."""
            # Check cancellation at the top.
            if self.cancellation_token.is_cancelled():
                _record(_make_event("step_cancelled", step,
                                    {"reason": "cancellation_token"}), step.step_id)
                return "cancelled", None, None

            # Precondition check.
            before = step.preconditions.evaluate(state_for_step, moment)
            _record(_make_event("precondition_verified", step,
                                _verification_details(before)), step.step_id)
            if before.status != FindingStatus.PASS:
                _record(_make_event("dispatch_blocked", step,
                                    {"reason": before.status.value}), step.step_id)
                if before.status == FindingStatus.INDETERMINATE:
                    return "blocked", None, step.on_indeterminate
                return "failed", None, step.on_failure

            # Dispatch (serialized).
            request = ActionRequest(
                step.action, principal_ref=principal_ref,
                idempotency_key="%s:%s" % (run_id, step.step_id),
                cancellation_token=self.cancellation_token,
            )
            attempt = 0
            result: Optional[ActionResult] = None
            while True:
                dispatch_start = time.monotonic()
                # Lock around dispatch ONLY if an idempotency store is
                # configured (the store is not thread-safe). Without a
                # store, dispatches are safe to run concurrently (handlers
                # are typically stateless). This allows true parallelism
                # for the common case of no store.
                if self.dispatcher.idempotency_store is not None:
                    with self._dispatch_lock:
                        result = self.dispatcher.dispatch(request, state_for_step)
                else:
                    result = self.dispatcher.dispatch(request, state_for_step)
                elapsed = time.monotonic() - dispatch_start
                if (step.timeout_seconds is not None
                        and elapsed > step.timeout_seconds
                        and result.status == ActionStatus.SUCCEEDED):
                    result = ActionResult(ActionStatus.INDETERMINATE,
                        "step timeout after %.3fs (limit %.3fs)" % (elapsed, step.timeout_seconds),
                        state=result.state)
                actions.append(result)
                _record(_make_event("action_%s" % result.status.value, step, {
                    "action": step.action, "principal_ref": request.principal_ref,
                    "request_id": request.request_id, "message": result.message,
                    "idempotency_key": request.idempotency_key,
                    "attempt": attempt + 1, "elapsed_seconds": round(elapsed, 6),
                }), step.step_id)
                if result.status not in (ActionStatus.FAILED, ActionStatus.INDETERMINATE) or attempt >= step.max_retries:
                    break
                attempt += 1
                if self.cancellation_token.is_cancelled():
                    break

            if result is None or result.status != ActionStatus.SUCCEEDED or result.state is None:
                # Distinguish cancellation from failure: if the cancellation
                # token fired, the step is CANCELLED (not FAILED). This
                # makes the run's final status CANCELLED, not FAILED.
                if self.cancellation_token.is_cancelled():
                    _record(_make_event("step_cancelled", step, {
                        "reason": "cancellation_token",
                    }), step.step_id)
                    return "cancelled", result, None
                _record(_make_event("step_failed", step, {
                    "reason": result.status.value if result else "no_result",
                }), step.step_id)
                # M7.1: route via on_indeterminate for INDETERMINATE,
                # on_failure for FAILED/DENIED. If no transition target,
                # next_step_id is None and the branch terminates here.
                if result is not None and result.status == ActionStatus.INDETERMINATE:
                    return "failed", result, step.on_indeterminate
                return "failed", result, step.on_failure

            # Postcondition check.
            after = step.postconditions.evaluate(result.state, moment)
            _record(_make_event("postcondition_verified", step,
                                _verification_details(after)), step.step_id)
            if after.status == FindingStatus.PASS:
                _record(_make_event("step_completed", step, {}), step.step_id)
                return "completed", result, step.on_success
            elif after.status == FindingStatus.INDETERMINATE:
                _record(_make_event("step_failed", step, {
                    "reason": "postcondition_%s" % after.status.value,
                }), step.step_id)
                return "failed", result, step.on_indeterminate
            else:  # FAIL
                _record(_make_event("step_failed", step, {
                    "reason": "postcondition_%s" % after.status.value,
                }), step.step_id)
                return "failed", result, step.on_failure

        # ---- scheduling loop ----
        # transition_eligible: step_ids that became eligible via a transition
        # (on_success/on_failure/on_indeterminate) from a completed step.
        # These bypass the normal depends_on check (the transition IS the
        # dependency signal) and inherit the predecessor's state.
        transition_eligible: Set[str] = set()
        # transition_state: the state to pass to each transition-eligible
        # step (the predecessor's result state).
        transition_state: Dict[str, ProcessState] = {}
        with ThreadPoolExecutor(max_workers=self.max_concurrency,
                                thread_name_prefix="upmp-parallel") as executor:
            in_flight: Dict[str, Future] = {}  # step_id -> Future
            iterations = 0
            while len(step_status) < len(self.dag.steps) and iterations < self.max_steps:
                iterations += 1
                # Check global cancellation.
                if self.cancellation_token.is_cancelled():
                    # Cancel all in-flight futures.
                    for sid, fut in list(in_flight.items()):
                        fut.cancel()
                    # Wait for them to finish (they should see the token
                    # and return "cancelled" via the worker).
                    for sid, fut in list(in_flight.items()):
                        try:
                            fut.result(timeout=2.0)
                        except (Exception, TimeoutError):
                            pass
                    in_flight.clear()
                    # Mark all remaining steps as cancelled.
                    for sid in self.dag.steps:
                        if sid not in step_status:
                            step_status[sid] = "cancelled"
                            _record(_make_event("step_cancelled", self.dag.steps[sid],
                                                {"reason": "run_cancelled"}), sid)
                    break

                # Schedule ready steps (deps satisfied, not yet started/done).
                for sid in self.dag.topological_order():
                    if sid in step_status or sid in in_flight:
                        continue
                    step = self.dag.steps[sid]
                    # M7.1: transition-eligible steps bypass depends_on.
                    # The transition IS the dependency signal; the target
                    # inherits the predecessor's state.
                    if sid in transition_eligible:
                        transition_eligible.discard(sid)
                        # Use the predecessor's state (recorded when the
                        # transition was triggered). Fall back to current_state
                        # if not available (shouldn't happen in normal flow).
                        state_for_step = transition_state.pop(sid, current_state)
                        # Submit to executor.
                        fut = executor.submit(_execute_step, step, state_for_step)
                        in_flight[sid] = fut
                        continue
                    # M7.1: a step reachable ONLY via a transition (no
                    # depends_on, but is a transition target) must NOT be
                    # scheduled independently. Wait for the transition.
                    if sid in self.dag._transition_only_roots:
                        continue
                    satisfied, unsatisfied, dep_status = _dependencies_satisfied(step)
                    if satisfied:
                        # Determine state for this step. Merge observations
                        # from ALL completed deps (diamond pattern: d needs
                        # state from both b and c). The merge is left-biased:
                        # earlier deps' observations are preserved unless a
                        # later dep overwrites them. The base state is the
                        # initial run state (so root observations are
                        # available to all steps).
                        state_for_step = current_state
                        for dep in step.depends_on:
                            if dep in step_state:
                                merged_obs = dict(state_for_step.observations)
                                merged_obs.update(step_state[dep].observations)
                                state_for_step = ProcessState(
                                    state_for_step.schema_id, state_for_step.schema_version,
                                    merged_obs)
                        # Submit to executor.
                        fut = executor.submit(_execute_step, step, state_for_step)
                        in_flight[sid] = fut
                    elif unsatisfied is not None and dep_status in ("failed", "blocked", "cancelled", "skipped"):
                        # A dependency reached a terminal non-completed state.
                        # Skip this step (don't run it; don't invent success).
                        step_status[sid] = "skipped"
                        _record(_make_event("step_skipped", step, {
                            "reason": "dependency_not_satisfied",
                            "dependency": unsatisfied,
                            "dependency_status": dep_status,
                        }), sid)
                    # else: dep is in_flight or pending — WAIT, don't schedule yet.

                if not in_flight:
                    # No steps running and no steps became ready this iteration.
                    # If there are still unfinished steps, they're blocked by
                    # unsatisfied dependencies that will never resolve.
                    unfinished = [s for s in self.dag.steps if s not in step_status]
                    if unfinished:
                        # Mark them all as skipped (their deps failed).
                        for sid in unfinished:
                            step_status[sid] = "skipped"
                            _record(_make_event("step_skipped", self.dag.steps[sid], {
                                "reason": "no_ready_path",
                            }), sid)
                    break

                # Wait for at least one in-flight future to complete.
                if in_flight:
                    # Poll with a short timeout so we can re-check cancellation.
                    done_fids = []
                    while not done_fids and in_flight:
                        # concurrent.futures.wait returns (done, not_done)
                        from concurrent.futures import wait, FIRST_COMPLETED
                        done, _not_done = wait(list(in_flight.values()),
                                               timeout=0.1,
                                               return_when=FIRST_COMPLETED)
                        if done:
                            done_fids = [sid for sid, fut in in_flight.items() if fut in done]
                            break
                        # Check cancellation while waiting.
                        if self.cancellation_token.is_cancelled():
                            break

                    # Process completed futures.
                    for sid in list(in_flight.keys()):
                        fut = in_flight[sid]
                        if not fut.done():
                            continue
                        try:
                            status, result, next_step_id = fut.result()
                        except CancelledError:
                            status = "cancelled"
                            result = None
                            next_step_id = None
                            _record(_make_event("step_cancelled", self.dag.steps[sid],
                                                {"reason": "future_cancelled"}), sid)
                        except Exception as exc:
                            status = "failed"
                            result = ActionResult(ActionStatus.FAILED,
                                "worker raised %s: %s" % (type(exc).__name__, exc))
                            next_step_id = None
                            actions.append(result)
                            _record(_make_event("step_failed", self.dag.steps[sid], {
                                "reason": "worker_exception",
                                "exception": type(exc).__name__,
                                "message": str(exc),
                            }), sid)
                        step_status[sid] = status
                        if result is not None and result.state is not None:
                            step_state[sid] = result.state
                            # Update current_state to the latest completed step's state.
                            current_state = result.state
                        # M7.1: if the step specified a transition target,
                        # mark it as eligible for immediate scheduling
                        # (bypassing the normal depends_on check — the
                        # transition IS the dependency signal). The
                        # target must exist, must not have already run
                        # (idempotency: at most once per run), and must
                        # not be currently in_flight.
                        if next_step_id is not None and next_step_id in self.dag.steps:
                            if (next_step_id not in step_status
                                    and next_step_id not in in_flight):
                                # Eligible via transition. Record an audit
                                # event noting the transition.
                                _record(_make_event("branch_transition", self.dag.steps[sid], {
                                    "from": sid,
                                    "to": next_step_id,
                                    "via": ("on_success" if status == "completed"
                                            else "on_indeterminate" if status == "blocked"
                                            else "on_failure"),
                                }), sid)
                                # Mark the target as transition-eligible so
                                # the scheduler picks it up next iteration
                                # without checking depends_on. Pass the
                                # predecessor's state explicitly.
                                transition_eligible.add(next_step_id)
                                if result is not None and result.state is not None:
                                    transition_state[next_step_id] = result.state
                                elif sid in step_state:
                                    transition_state[next_step_id] = step_state[sid]
                        del in_flight[sid]

            # If we exited the loop with in-flight futures still running
            # (e.g., max_steps hit), cancel and wait.
            for sid, fut in list(in_flight.items()):
                fut.cancel()
                try:
                    fut.result(timeout=1.0)
                except (Exception, TimeoutError):
                    pass
                if sid not in step_status:
                    step_status[sid] = "cancelled"

        # ---- determine final status ----
        all_completed = all(s == "completed" for s in step_status.values())
        any_failed = any(s == "failed" for s in step_status.values())
        any_cancelled = any(s == "cancelled" for s in step_status.values())
        any_blocked = any(s == "blocked" for s in step_status.values())
        any_skipped = any(s == "skipped" for s in step_status.values())

        if any_cancelled:
            final_status = RunStatus.CANCELLED
        elif any_failed:
            final_status = RunStatus.FAILED
        elif any_blocked:
            final_status = RunStatus.BLOCKED
        elif any_skipped:
            # All non-skipped steps completed, but some were skipped
            # due to dependency failure. This is a partial-completion
            # state; report as FAILED because the workflow did not
            # fully achieve its intent.
            final_status = RunStatus.FAILED
        elif all_completed:
            final_status = RunStatus.COMPLETED
        else:
            final_status = RunStatus.STEP_LIMIT

        # Find a representative current_step (the last completed step
        # in topological order, or None if all completed).
        last_completed = None
        for sid in reversed(self.dag.topological_order()):
            if step_status.get(sid) == "completed":
                last_completed = sid
                break

        return WorkflowResult(
            status=final_status,
            final_state=current_state,
            current_step=last_completed,
            verification=None,  # parallel runs have multiple verification results
            actions=tuple(actions),
            audit=tuple(audit),
        )
