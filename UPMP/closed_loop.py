"""A small, auditable closed-loop runtime for declarative UPMP workflows.

This runtime only invokes explicitly registered and allow-listed handlers. It
is a coordination primitive, not a safety-rated controller.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence

from .contracts import FindingStatus, ProcessState, VerificationPolicy, VerificationResult
from .cancellation import CancellationToken, NEVER_CANCELLED
from .idempotency import IdempotencyStore, InMemoryIdempotencyStore

if TYPE_CHECKING:
    from .audit import AuditSink


class ActionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    INDETERMINATE = "indeterminate"


class RunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SCHEDULED = "scheduled"
    STEP_LIMIT = "step_limit"
    CANCELLED = "cancelled"   # M2: external cancellation requested via CancellationToken
    TIMEOUT = "timeout"       # M2: run-level deadline exceeded


@dataclass(frozen=True)
class ActionRequest:
    action: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    principal_ref: Optional[str] = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    idempotency_key: str = ""
    # M2: cooperative cancellation token. Handlers that support
    # cancellation poll request.cancellation_token.is_cancelled()
    # periodically and exit early when True. Default NEVER_CANCELLED
    # preserves backward compatibility for handlers that never check.
    cancellation_token: CancellationToken = field(default=NEVER_CANCELLED)

    def __post_init__(self) -> None:
        if not self.action:
            raise ValueError("action is required")


@dataclass(frozen=True)
class ActionResult:
    status: ActionStatus
    message: str
    state: Optional[ProcessState] = None
    output: Mapping[str, Any] = field(default_factory=dict)
    completed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if not isinstance(self.status, ActionStatus):
            object.__setattr__(self, "status", ActionStatus(self.status))
        try:
            stamp = datetime.fromisoformat(self.completed_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timestamp must include a timezone")
        except (AttributeError, ValueError) as exc:
            raise ValueError("completed_at must be timezone-aware ISO-8601") from exc


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    action: str
    preconditions: VerificationPolicy
    postconditions: VerificationPolicy
    on_success: Optional[str] = None
    on_failure: Optional[str] = None
    on_indeterminate: Optional[str] = None
    not_before: Optional[str] = None
    max_retries: int = 0
    idempotent: bool = False
    description: str = ""
    # M2: per-step timeout in seconds (wall-clock). If the handler
    # takes longer than this, the result is treated as INDETERMINATE
    # with reason "timeout" and routed via on_indeterminate. This is
    # a POST-HOC check — it does NOT preempt the handler. Handlers
    # that wish to exit early on timeout should poll the
    # cancellation_token on the ActionRequest.
    timeout_seconds: Optional[float] = None
    # M7: static scheduling dependencies for parallel DAG execution.
    # A step with depends_on=[A, B] will not start until BOTH A and B
    # have reached a terminal state. By "terminal state" the
    # ParallelOrchestrator means: the dependency's branch reached
    # COMPLETED status. If any dependency did NOT complete (FAILED,
    # BLOCKED, CANCELLED, etc.), the dependent step is SKIPPED and an
    # audit event of type "step_skipped" is recorded.
    #
    # depends_on is ORTHOGONAL to on_success/on_failure/on_indeterminate:
    #   - depends_on = static scheduling constraint (parallel)
    #   - on_success/on_failure/on_indeterminate = per-step branch routing
    #     (sequential within a step's own sub-chain)
    #
    # ClosedLoopOrchestrator (sequential) IGNORES depends_on entirely,
    # preserving 100% backward compatibility with pre-M7 workflows.
    depends_on: Sequence[str] = ()

    def __post_init__(self) -> None:
        if not self.step_id or not self.action:
            raise ValueError("step_id and action are required")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.max_retries and not self.idempotent:
            raise ValueError("retries require an action declared idempotent")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive or None")
        if self.not_before:
            parsed = datetime.fromisoformat(self.not_before.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("not_before must include a timezone")


@dataclass(frozen=True)
class AuditEvent:
    event: str
    step_id: str
    timestamp: str
    details: Mapping[str, Any] = field(default_factory=dict)
    # Chain metadata — assigned by AuditSink at append() time. Defaults
    # preserve backward compatibility: events constructed without these
    # fields chain correctly when passed through an AuditSink. See
    # UPMP/audit.py (M1.1).
    sequence_number: int = 0
    prev_hash: str = ""
    # M7: which parallel branch this event came from. Default "default"
    # preserves backward compat for ClosedLoopOrchestrator (sequential,
    # single branch). ParallelOrchestrator sets branch_id = step_id for
    # each event so a reader of the merged audit trail can reconstruct
    # causality across concurrent branches. The field is part of the
    # canonical_json payload (hash-covered) so tampering with branch_id
    # is detectable.
    branch_id: str = "default"


@dataclass(frozen=True)
class WorkflowResult:
    status: RunStatus
    final_state: ProcessState
    current_step: Optional[str]
    verification: Optional[VerificationResult]
    actions: Sequence[ActionResult]
    audit: Sequence[AuditEvent]


ActionHandler = Callable[[ActionRequest, ProcessState], ActionResult]
AuthorizationCheck = Callable[[ActionRequest, ProcessState], bool]


class ActionDispatcher:
    """Enforces a static action allow-list and records every dispatch result.

    M3 — when an idempotency_store is configured, dispatches with a
    non-empty idempotency_key are deduplicated at the dispatcher layer:
    the first dispatch caches the result; subsequent dispatches with
    the same (action, key) return the cached result WITHOUT re-invoking
    the handler. This ENFORCES idempotency at the framework level,
    removing the burden from individual handlers.

    When no store is configured (the default), behavior is unchanged:
    every dispatch invokes the handler, and the handler is responsible
    for deduplication (per the pre-M3 contract).
    """

    def __init__(self, allowed_actions: Sequence[str] = (),
                 authorize: Optional[AuthorizationCheck] = None,
                 idempotency_store: Optional[IdempotencyStore] = None) -> None:
        self.allowed_actions = frozenset(allowed_actions)
        self.authorize = authorize
        self.idempotency_store = idempotency_store
        self._handlers: Dict[str, ActionHandler] = {}

    def register(self, name: str, handler: ActionHandler) -> None:
        if not name or name not in self.allowed_actions:
            raise ValueError("action must be explicitly allow-listed")
        if name in self._handlers:
            raise ValueError("action handler already registered: %s" % name)
        self._handlers[name] = handler

    def dispatch(self, request: ActionRequest, state: ProcessState) -> ActionResult:
        if request.action not in self.allowed_actions or request.action not in self._handlers:
            return ActionResult(ActionStatus.DENIED, "action is not authorized")
        # M3: if an idempotency_store is configured and the request
        # carries an idempotency_key, check the store BEFORE
        # authorization or handler invocation. A cached result is
        # returned as-is. This is the framework-level enforcement of
        # idempotency: the handler is never re-invoked for the same key.
        if self.idempotency_store is not None and request.idempotency_key:
            cached = self.idempotency_store.get(request.action, request.idempotency_key)
            if cached is not None:
                return cached
        if self.authorize is not None:
            try:
                if not self.authorize(request, state):
                    return ActionResult(ActionStatus.DENIED, "external authorization policy denied action")
            except Exception:
                return ActionResult(ActionStatus.DENIED, "external authorization policy unavailable")
        try:
            result = self._handlers[request.action](request, state)
            if not isinstance(result, ActionResult):
                result = ActionResult(ActionStatus.INDETERMINATE, "handler returned an invalid result")
        except Exception as exc:
            result = ActionResult(ActionStatus.FAILED, "action handler raised %s: %s" % (type(exc).__name__, exc))
        # M3: cache the result for future dispatches with the same key.
        # All terminal statuses (SUCCEEDED, FAILED, DENIED, INDETERMINATE)
        # are cached — a failed action is idempotent-failed, not retried
        # by a higher layer relying on the store.
        if self.idempotency_store is not None and request.idempotency_key:
            self.idempotency_store.put(request.action, request.idempotency_key, result)
        return result


class ClosedLoopOrchestrator:
    """Bounded closed-loop runtime with optional durable audit integration.

    M1.5 — audit_sink: when an AuditSink is provided, every audit event
    generated during run() is also appended to the sink, which assigns
    sequence numbers and hash-chain metadata. WorkflowResult.audit
    still contains the plain (unchained) events for backward
    compatibility; the sink holds the chained, verifiable copies.

    Failure policy (fail-loud): if sink.append() raises, the exception
    propagates and run() aborts WITHOUT returning a WorkflowResult.
    Events already accepted by the sink remain recorded. This prevents
    the framework from reporting a completed run whose evidence trail
    is silently incomplete. Callers who prefer availability over
    evidence completeness can wrap the sink in a fail-open adapter —
    but the default is fail-loud.
    """

    def __init__(self, steps: Sequence[WorkflowStep], dispatcher: ActionDispatcher,
                 max_steps: int = 100,
                 audit_sink: Optional["AuditSink"] = None,
                 cancellation_token: Optional[CancellationToken] = None) -> None:
        if not steps:
            raise ValueError("workflow needs at least one step")
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        if audit_sink is not None and not callable(getattr(audit_sink, "append", None)):
            raise TypeError("audit_sink must provide append(event) -> int")
        self.audit_sink = audit_sink
        # M2: bind the run-level cancellation token. None means "use
        # NEVER_CANCELLED" — handlers see a token that is never set, so
        # existing code paths that never check the token behave identically
        # to pre-M2 UPMP.
        self.cancellation_token = cancellation_token if cancellation_token is not None else NEVER_CANCELLED
        self.steps = {s.step_id: s for s in steps}
        if len(self.steps) != len(steps):
            raise ValueError("step ids must be unique")
        self.start_step = steps[0].step_id
        self.dispatcher = dispatcher
        self.max_steps = max_steps
        for step in steps:
            for target in (step.on_success, step.on_failure, step.on_indeterminate):
                if target is not None and target not in self.steps:
                    raise ValueError("transition target does not exist: %s" % target)

    def run(self, initial_state: ProcessState, now: Optional[datetime] = None,
            principal_ref: Optional[str] = None,
            deadline: Optional[datetime] = None) -> WorkflowResult:
        """Execute the workflow. Audit events are recorded to the run's
        trail and, when audit_sink was configured, to the durable sink
        (sink failures abort the run — fail-loud policy, see class
        docstring).

        M2 cancellation/timeout:
          - The run-level cancellation_token (set on the orchestrator)
            is checked at the top of each step iteration. If cancelled,
            the run returns RunStatus.CANCELLED with the audit trail up
            to the cancellation point.
          - The optional `deadline` (timezone-aware datetime) is also
            checked at the top of each step iteration. If exceeded, the
            run returns RunStatus.TIMEOUT.
          - Per-step timeout_seconds (set on WorkflowStep) is checked
            AFTER dispatch returns; if exceeded, the result is treated
            as INDETERMINATE with reason "timeout" and routed via
            on_indeterminate.

        Limitation: cancellation and timeout are COOPERATIVE. A handler
        that ignores the cancellation_token and never returns cannot
        be preempted. True preemption requires thread/process isolation
        (deferred to M2.2)."""
        if now is not None and now.tzinfo is None:
            raise ValueError("now must include a timezone")
        if deadline is not None and deadline.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        current = initial_state
        step_id: Optional[str] = self.start_step
        last_verification: Optional[VerificationResult] = None
        actions: List[ActionResult] = []
        audit: List[AuditEvent] = []
        run_id = uuid.uuid4().hex
        audit_sink = self.audit_sink

        def _record(event: AuditEvent) -> AuditEvent:
            audit.append(event)
            if audit_sink is not None:
                audit_sink.append(event)
            return event

        for _ in range(self.max_steps):
            # M2: check run-level cancellation and deadline at the top
            # of each step iteration. These are checked BEFORE the step
            # is fetched, so a cancelled or expired run does not execute
            # any further steps.
            if self.cancellation_token.is_cancelled():
                _record(self._event("run_cancelled", step_id and self.steps.get(step_id) or self.steps[self.start_step], {"reason": "cancellation_token"}))
                return WorkflowResult(RunStatus.CANCELLED, current, step_id, last_verification, tuple(actions), tuple(audit))
            moment_now = now or datetime.now(timezone.utc)
            if deadline is not None and moment_now >= deadline:
                _record(self._event("run_timeout", step_id and self.steps.get(step_id) or self.steps[self.start_step], {"reason": "deadline_exceeded", "deadline": deadline.isoformat()}))
                return WorkflowResult(RunStatus.TIMEOUT, current, step_id, last_verification, tuple(actions), tuple(audit))
            if step_id is None:
                return WorkflowResult(RunStatus.COMPLETED, current, None, last_verification, tuple(actions), tuple(audit))
            step = self.steps[step_id]
            moment = moment_now
            if step.not_before:
                scheduled = datetime.fromisoformat(step.not_before.replace("Z", "+00:00"))
                if moment < scheduled:
                    _record(self._event("scheduled", step, {"not_before": step.not_before}))
                    return WorkflowResult(RunStatus.SCHEDULED, current, step_id, last_verification, tuple(actions), tuple(audit))

            before = step.preconditions.evaluate(current, moment)
            last_verification = before
            _record(self._event("precondition_verified", step, self._verification_details(before)))
            if before.status != FindingStatus.PASS:
                _record(self._event("dispatch_blocked", step, {"reason": before.status.value}))
                step_id = step.on_indeterminate if before.status == FindingStatus.INDETERMINATE else step.on_failure
                if step_id is None:
                    status = RunStatus.BLOCKED if before.status == FindingStatus.INDETERMINATE else RunStatus.FAILED
                    return WorkflowResult(status, current, step.step_id, before, tuple(actions), tuple(audit))
                continue

            request = ActionRequest(
                step.action, principal_ref=principal_ref,
                idempotency_key="%s:%s" % (run_id, step.step_id),
                cancellation_token=self.cancellation_token,
            )
            attempt = 0
            while True:
                dispatch_start = time.monotonic()
                result = self.dispatcher.dispatch(request, current)
                elapsed = time.monotonic() - dispatch_start
                # M2: per-step timeout check (post-hoc, not preemptive).
                # If the handler took longer than step.timeout_seconds,
                # treat the result as INDETERMINATE with reason "timeout"
                # and route via on_indeterminate. This does NOT preempt
                # the handler — it only changes how the (already-returned)
                # result is interpreted.
                if (step.timeout_seconds is not None
                        and elapsed > step.timeout_seconds
                        and result.status == ActionStatus.SUCCEEDED):
                    result = ActionResult(ActionStatus.INDETERMINATE,
                        "step timeout after %.3fs (limit %.3fs)" % (elapsed, step.timeout_seconds),
                        state=result.state)
                actions.append(result)
                _record(self._event("action_%s" % result.status.value, step, {"action": step.action, "principal_ref": request.principal_ref, "request_id": request.request_id, "message": result.message, "idempotency_key": request.idempotency_key, "attempt": attempt + 1, "elapsed_seconds": round(elapsed, 6)}))
                if result.status not in (ActionStatus.FAILED, ActionStatus.INDETERMINATE) or attempt >= step.max_retries:
                    break
                attempt += 1
            if result.status != ActionStatus.SUCCEEDED or result.state is None:
                branch = step.on_indeterminate if result.status == ActionStatus.INDETERMINATE else step.on_failure
                if result.status == ActionStatus.DENIED:
                    branch = step.on_failure
                if branch is None:
                    status = RunStatus.BLOCKED if result.status in (ActionStatus.DENIED, ActionStatus.INDETERMINATE) else RunStatus.FAILED
                    return WorkflowResult(status, current, step.step_id, last_verification, tuple(actions), tuple(audit))
                step_id = branch
                continue

            current = result.state
            after = step.postconditions.evaluate(current, moment)
            last_verification = after
            _record(self._event("postcondition_verified", step, self._verification_details(after)))
            if after.status == FindingStatus.PASS:
                step_id = step.on_success
            elif after.status == FindingStatus.FAIL:
                step_id = step.on_failure
            else:
                step_id = step.on_indeterminate
            if step_id is None:
                if after.status == FindingStatus.PASS:
                    return WorkflowResult(RunStatus.COMPLETED, current, None, after, tuple(actions), tuple(audit))
                status = RunStatus.BLOCKED if after.status == FindingStatus.INDETERMINATE else RunStatus.FAILED
                return WorkflowResult(status, current, step.step_id, after, tuple(actions), tuple(audit))
        return WorkflowResult(RunStatus.STEP_LIMIT, current, step_id, last_verification, tuple(actions), tuple(audit))

    @staticmethod
    def _event(name: str, step: WorkflowStep, details: Mapping[str, Any]) -> AuditEvent:
        return AuditEvent(name, step.step_id, datetime.now(timezone.utc).isoformat(), dict(details))

    @staticmethod
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

