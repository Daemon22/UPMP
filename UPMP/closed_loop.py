"""A small, auditable closed-loop runtime for declarative UPMP workflows.

This runtime only invokes explicitly registered and allow-listed handlers. It
is a coordination primitive, not a safety-rated controller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .contracts import FindingStatus, ProcessState, VerificationPolicy, VerificationResult


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


@dataclass(frozen=True)
class ActionRequest:
    action: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    principal_ref: Optional[str] = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    idempotency_key: str = ""

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

    def __post_init__(self) -> None:
        if not self.step_id or not self.action:
            raise ValueError("step_id and action are required")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.max_retries and not self.idempotent:
            raise ValueError("retries require an action declared idempotent")
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
    """Enforces a static action allow-list and records every dispatch result."""

    def __init__(self, allowed_actions: Sequence[str] = (),
                 authorize: Optional[AuthorizationCheck] = None) -> None:
        self.allowed_actions = frozenset(allowed_actions)
        self.authorize = authorize
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
        if self.authorize is not None:
            try:
                if not self.authorize(request, state):
                    return ActionResult(ActionStatus.DENIED, "external authorization policy denied action")
            except Exception:
                return ActionResult(ActionStatus.DENIED, "external authorization policy unavailable")
        try:
            result = self._handlers[request.action](request, state)
            if not isinstance(result, ActionResult):
                return ActionResult(ActionStatus.INDETERMINATE, "handler returned an invalid result")
            return result
        except Exception as exc:
            return ActionResult(ActionStatus.FAILED, "action handler raised %s: %s" % (type(exc).__name__, exc))


class ClosedLoopOrchestrator:
    def __init__(self, steps: Sequence[WorkflowStep], dispatcher: ActionDispatcher,
                 max_steps: int = 100) -> None:
        if not steps:
            raise ValueError("workflow needs at least one step")
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
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
            principal_ref: Optional[str] = None) -> WorkflowResult:
        if now is not None and now.tzinfo is None:
            raise ValueError("now must include a timezone")
        current = initial_state
        step_id: Optional[str] = self.start_step
        last_verification: Optional[VerificationResult] = None
        actions: List[ActionResult] = []
        audit: List[AuditEvent] = []
        run_id = uuid.uuid4().hex
        for _ in range(self.max_steps):
            if step_id is None:
                return WorkflowResult(RunStatus.COMPLETED, current, None, last_verification, tuple(actions), tuple(audit))
            step = self.steps[step_id]
            moment = now or datetime.now(timezone.utc)
            if step.not_before:
                scheduled = datetime.fromisoformat(step.not_before.replace("Z", "+00:00"))
                if moment < scheduled:
                    audit.append(self._event("scheduled", step, {"not_before": step.not_before}))
                    return WorkflowResult(RunStatus.SCHEDULED, current, step_id, last_verification, tuple(actions), tuple(audit))

            before = step.preconditions.evaluate(current, moment)
            last_verification = before
            audit.append(self._event("precondition_verified", step, self._verification_details(before)))
            if before.status != FindingStatus.PASS:
                audit.append(self._event("dispatch_blocked", step, {"reason": before.status.value}))
                step_id = step.on_indeterminate if before.status == FindingStatus.INDETERMINATE else step.on_failure
                if step_id is None:
                    status = RunStatus.BLOCKED if before.status == FindingStatus.INDETERMINATE else RunStatus.FAILED
                    return WorkflowResult(status, current, step.step_id, before, tuple(actions), tuple(audit))
                continue

            request = ActionRequest(
                step.action, principal_ref=principal_ref,
                idempotency_key="%s:%s" % (run_id, step.step_id),
            )
            attempt = 0
            while True:
                result = self.dispatcher.dispatch(request, current)
                actions.append(result)
                audit.append(self._event("action_%s" % result.status.value, step, {"action": step.action, "principal_ref": request.principal_ref, "request_id": request.request_id, "message": result.message, "idempotency_key": request.idempotency_key, "attempt": attempt + 1}))
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
            audit.append(self._event("postcondition_verified", step, self._verification_details(after)))
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

