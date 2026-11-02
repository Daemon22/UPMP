"""Step-by-step diagnosis for UPMP workflow results.

Diagnosis is a pure, dependency-free view over WorkflowResult.audit. It
summarizes where a run passed, failed, was blocked, or lacks enough evidence.
Recommendations are checks for a human or calling system; they never execute
repairs or claim to prove a root cause.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple

from .closed_loop import RunStatus, WorkflowResult


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class StepOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INDETERMINATE = "indeterminate"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    SCHEDULED = "scheduled"
    UNKNOWN = "unknown"


class DiagnosticCode(str, Enum):
    PRECONDITION_FAILED = "precondition_failed"
    PRECONDITION_INDETERMINATE = "precondition_indeterminate"
    ACTION_FAILED = "action_failed"
    ACTION_DENIED = "action_denied"
    ACTION_INDETERMINATE = "action_indeterminate"
    POSTCONDITION_FAILED = "postcondition_failed"
    POSTCONDITION_INDETERMINATE = "postcondition_indeterminate"
    POSTCONDITION_UNVERIFIED = "postcondition_unverified"
    DEPENDENCY_NOT_SATISFIED = "dependency_not_satisfied"
    STEP_FAILED = "step_failed"
    STEP_CANCELLED = "step_cancelled"
    DISPATCH_BLOCKED = "dispatch_blocked"
    RUN_CANCELLED = "run_cancelled"
    RUN_TIMEOUT = "run_timeout"
    STEP_LIMIT_REACHED = "step_limit_reached"
    WORKFLOW_FAILED = "workflow_failed"
    WORKFLOW_BLOCKED = "workflow_blocked"
    NO_STEP_EVIDENCE = "no_step_evidence"
    STEP_OUTCOME_UNVERIFIED = "step_outcome_unverified"


@dataclass(frozen=True)
class WorkflowIssue:
    """An observed failure signal and a suggested next check."""

    code: DiagnosticCode
    severity: DiagnosticSeverity
    stage: str
    message: str
    recommendation: str
    step_id: Optional[str] = None
    branch_id: Optional[str] = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "stage": self.stage,
            "step_id": self.step_id,
            "branch_id": self.branch_id,
            "message": self.message,
            "recommendation": self.recommendation,
            "evidence": _json_safe(self.evidence),
        }


@dataclass(frozen=True)
class StepDiagnosis:
    """Observed outcome for one workflow step or parallel branch."""

    step_id: str
    branch_id: str
    action: Optional[str]
    outcome: StepOutcome
    attempts: int
    issues: Tuple[WorkflowIssue, ...]

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "step_id": self.step_id,
            "branch_id": self.branch_id,
            "action": self.action,
            "outcome": self.outcome.value,
            "attempts": self.attempts,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class WorkflowDiagnosis:
    """Human-readable and machine-readable diagnosis of a workflow run."""

    run_status: str
    summary: str
    steps: Tuple[StepDiagnosis, ...]
    run_issues: Tuple[WorkflowIssue, ...]

    @property
    def issues(self) -> Tuple[WorkflowIssue, ...]:
        return tuple(issue for step in self.steps for issue in step.issues) + self.run_issues

    @property
    def needs_attention(self) -> bool:
        return any(issue.severity != DiagnosticSeverity.INFO for issue in self.issues)

    def to_dict(self) -> Mapping[str, Any]:
        """Return a JSON-compatible report without dispatcher credentials."""
        return {
            "run_status": self.run_status,
            "summary": self.summary,
            "needs_attention": self.needs_attention,
            "steps": [step.to_dict() for step in self.steps],
            "run_issues": [issue.to_dict() for issue in self.run_issues],
        }

    def format_report(self) -> str:
        """Format a concise text report suitable for logs or a CLI."""
        lines = [self.summary, "Run status: %s" % self.run_status]
        for step in self.steps:
            action = " — %s" % step.action if step.action else ""
            branch = " (branch %s)" % step.branch_id if step.branch_id != "default" else ""
            attempts = " — %d attempt%s" % (step.attempts, "" if step.attempts == 1 else "s") if step.attempts else ""
            lines.append("[%s] %s%s%s%s" % (
                step.outcome.value.upper(), step.step_id, action, branch, attempts))
            for issue in step.issues:
                lines.append("  %s %s: %s" % (
                    issue.severity.value.upper(), issue.code.value, issue.message))
                lines.append("    Next check: %s" % issue.recommendation)
        for issue in self.run_issues:
            label = " at step %s" % issue.step_id if issue.step_id else ""
            lines.append("%s%s: %s" % (
                issue.code.value, label, issue.message))
            lines.append("  Next check: %s" % issue.recommendation)
        return "\n".join(lines)


def diagnose_workflow(result: WorkflowResult) -> WorkflowDiagnosis:
    """Summarize per-step outcomes and likely next checks from a run result.

    This works with sequential and parallel orchestrators because both record
    the same step-level audit events. It reports observed signals, not proven
    root causes, and it never dispatches an action.
    """
    grouped = {}
    run_issues = []
    audit = tuple(getattr(result, "audit", ()) or ())

    for event in audit:
        event_name = str(getattr(event, "event", ""))
        step_id = str(getattr(event, "step_id", "") or "")
        branch_id = str(getattr(event, "branch_id", "default") or "default")
        if event_name in ("run_cancelled", "run_timeout"):
            run_issues.append(_run_issue(event_name, step_id or None, branch_id, event))
            continue
        if not step_id:
            continue
        key = (branch_id, step_id)
        grouped.setdefault(key, []).append(event)

    steps = tuple(
        _diagnose_step(step_id, branch_id, events)
        for (branch_id, step_id), events in grouped.items()
    )

    run_status = _status_value(getattr(result, "status", "unknown"))
    if run_status == RunStatus.STEP_LIMIT.value:
        run_issues.append(_issue(
            DiagnosticCode.STEP_LIMIT_REACHED, DiagnosticSeverity.ERROR, "workflow",
            "The workflow stopped after reaching its configured step limit.",
            "Inspect the transition path for an unintended loop, or raise max_steps if the longer run is intentional.",
            getattr(result, "current_step", None), None,
            {"current_step": getattr(result, "current_step", None)},
        ))
    elif run_status == RunStatus.TIMEOUT.value and not any(i.code == DiagnosticCode.RUN_TIMEOUT for i in run_issues):
        run_issues.append(_issue(
            DiagnosticCode.RUN_TIMEOUT, DiagnosticSeverity.ERROR, "workflow",
            "The workflow deadline expired before the run completed.",
            "Inspect the active step and deadline; reconcile external side effects before retrying.",
            getattr(result, "current_step", None), None,
            {"current_step": getattr(result, "current_step", None)},
        ))
    elif run_status == RunStatus.CANCELLED.value and not any(i.code == DiagnosticCode.RUN_CANCELLED for i in run_issues):
        run_issues.append(_issue(
            DiagnosticCode.RUN_CANCELLED, DiagnosticSeverity.WARNING, "workflow",
            "The workflow was cancelled before the run completed.",
            "Check why cancellation was requested and reconcile any action that was still in flight.",
            getattr(result, "current_step", None), None,
            {"current_step": getattr(result, "current_step", None)},
        ))
    elif run_status == RunStatus.FAILED.value and not any(step.issues for step in steps):
        run_issues.append(_issue(
            DiagnosticCode.WORKFLOW_FAILED, DiagnosticSeverity.ERROR, "workflow",
            "The run failed, but its step evidence does not identify a specific failure.",
            "Inspect the complete audit trail and the current step for missing failure evidence.",
            getattr(result, "current_step", None), None,
            {"current_step": getattr(result, "current_step", None)},
        ))
    elif run_status == RunStatus.BLOCKED.value and not any(step.issues for step in steps):
        run_issues.append(_issue(
            DiagnosticCode.WORKFLOW_BLOCKED, DiagnosticSeverity.ERROR, "workflow",
            "The run was blocked, but its step evidence does not identify the blocking condition.",
            "Inspect the complete audit trail and verify the observations and authorization inputs.",
            getattr(result, "current_step", None), None,
            {"current_step": getattr(result, "current_step", None)},
        ))

    if run_status == RunStatus.COMPLETED.value and not steps:
        run_issues.append(_issue(
            DiagnosticCode.NO_STEP_EVIDENCE, DiagnosticSeverity.WARNING, "workflow",
            "The run reports completion but contains no step-level audit evidence.",
            "Check that the orchestrator returned its audit trail and that audit events were retained.",
            None, None, {},
        ))

    all_issues = tuple(issue for step in steps for issue in step.issues) + tuple(run_issues)
    summary = _summary(run_status, len(steps), all_issues)
    return WorkflowDiagnosis(run_status, summary, steps, tuple(run_issues))


def _diagnose_step(step_id: str, branch_id: str, events: Sequence[Any]) -> StepDiagnosis:
    issues = []
    action_events = [event for event in events if _event_name(event) in (
        "action_succeeded", "action_failed", "action_denied", "action_indeterminate")]
    attempts = len(action_events)
    action = None
    for event in action_events:
        action = _details(event).get("action") or action
    precondition_events = [event for event in events if _event_name(event) == "precondition_verified"]
    postcondition_events = [event for event in events if _event_name(event) == "postcondition_verified"]

    for event in precondition_events:
        details = _details(event)
        status = _status_value(details.get("status", "indeterminate"))
        if status != "pass":
            code = (DiagnosticCode.PRECONDITION_FAILED if status == "fail"
                    else DiagnosticCode.PRECONDITION_INDETERMINATE)
            issues.append(_verification_issue(code, "precondition", step_id, branch_id, event))

    for event in action_events:
        name = _event_name(event)
        if name == "action_succeeded":
            continue
        details = _details(event)
        if name == "action_failed":
            failure_index = _event_index(events, event)
            recovered = any(
                _event_name(candidate) == "action_succeeded"
                and _event_index(events, candidate) > failure_index
                and any(
                    _event_name(verified) == "postcondition_verified"
                    and _status_value(_details(verified).get("status")) == "pass"
                    and _event_index(events, verified) > _event_index(events, candidate)
                    for verified in postcondition_events
                )
                for candidate in action_events
            )
            message = str(details.get("message") or "The action handler failed.")
            if recovered:
                message += " A later attempt passed postcondition verification."
            issues.append(_issue(
                DiagnosticCode.ACTION_FAILED,
                DiagnosticSeverity.WARNING if recovered else DiagnosticSeverity.ERROR,
                "action", message,
                ("Review the transient handler error; a later attempt succeeded and passed verification."
                 if recovered else
                 "Inspect the handler error and the external system state; fix the cause before retrying."),
                step_id, branch_id, _event_evidence(event),
            ))
        elif name == "action_denied":
            issues.append(_issue(
                DiagnosticCode.ACTION_DENIED, DiagnosticSeverity.ERROR, "authorization",
                str(details.get("message") or "The action was denied."),
                "Check the action allow-list and the external authorization decision; do not bypass either control.",
                step_id, branch_id, _event_evidence(event),
            ))
        else:
            issues.append(_issue(
                DiagnosticCode.ACTION_INDETERMINATE, DiagnosticSeverity.ERROR, "action",
                str(details.get("message") or "The action outcome is indeterminate."),
                "Reconcile whether the external action took effect before retrying.",
                step_id, branch_id, _event_evidence(event),
            ))

    for event in postcondition_events:
        details = _details(event)
        status = _status_value(details.get("status", "indeterminate"))
        if status != "pass":
            code = (DiagnosticCode.POSTCONDITION_FAILED if status == "fail"
                    else DiagnosticCode.POSTCONDITION_INDETERMINATE)
            event_index = _event_index(events, event)
            recovered = any(
                _status_value(_details(candidate).get("status")) == "pass"
                and _event_index(events, candidate) > event_index
                for candidate in postcondition_events
            )
            severity = DiagnosticSeverity.WARNING if recovered else DiagnosticSeverity.ERROR
            issue = _verification_issue(code, "postcondition", step_id, branch_id, event, severity)
            if recovered:
                issue = WorkflowIssue(
                    issue.code, issue.severity, issue.stage,
                    issue.message + " A later verification passed.",
                    "Review the earlier failure; the step later passed verification.",
                    issue.step_id, issue.branch_id, issue.evidence,
                )
            issues.append(issue)

    if any(_event_name(event) == "dispatch_blocked" for event in events) and not any(
            issue.code in (DiagnosticCode.PRECONDITION_FAILED,
                           DiagnosticCode.PRECONDITION_INDETERMINATE)
            for issue in issues):
        event = next(event for event in events if _event_name(event) == "dispatch_blocked")
        details = _details(event)
        issues.append(_issue(
            DiagnosticCode.DISPATCH_BLOCKED, DiagnosticSeverity.ERROR, "precondition",
            "Dispatch was blocked: %s." % str(details.get("reason", "condition not met")),
            "Inspect the precondition evidence and refresh or correct the observations before rerunning.",
            step_id, branch_id, _event_evidence(event),
        ))

    skipped = [event for event in events if _event_name(event) == "step_skipped"]
    for event in skipped:
        details = _details(event)
        dependency = details.get("dependency")
        dependency_status = details.get("dependency_status")
        target = "Dependency %s" % dependency if dependency else "A required dependency"
        if dependency_status:
            target += " ended %s" % dependency_status
        issues.append(_issue(
            DiagnosticCode.DEPENDENCY_NOT_SATISFIED, DiagnosticSeverity.WARNING, "dependency",
            "%s did not complete, so this step was skipped." % target,
            "Fix or inspect the dependency first; this step was not attempted.",
            step_id, branch_id, _event_evidence(event),
        ))

    cancelled = [event for event in events if _event_name(event) == "step_cancelled"]
    for event in cancelled:
        details = _details(event)
        issues.append(_issue(
            DiagnosticCode.STEP_CANCELLED, DiagnosticSeverity.WARNING, "execution",
            "The step was cancelled: %s." % str(details.get("reason", "cancellation requested")),
            "Check why cancellation was requested and reconcile any action that was still in flight.",
            step_id, branch_id, _event_evidence(event),
        ))

    terminal_events = [event for event in events if _event_name(event) in (
        "step_completed", "step_failed", "step_skipped", "step_cancelled")]
    if terminal_events and _event_name(terminal_events[-1]) == "step_failed" and not issues:
        event = terminal_events[-1]
        details = _details(event)
        issues.append(_issue(
            DiagnosticCode.STEP_FAILED, DiagnosticSeverity.ERROR, "execution",
            "The step failed: %s." % str(details.get("reason", "no reason was recorded")),
            "Inspect this step's action and verification evidence before rerunning it.",
            step_id, branch_id, _event_evidence(event),
        ))

    has_action_success = any(_event_name(event) == "action_succeeded" for event in action_events)
    if has_action_success and not postcondition_events and not any(
            issue.code == DiagnosticCode.POSTCONDITION_UNVERIFIED for issue in issues):
        event = next(event for event in reversed(action_events)
                     if _event_name(event) == "action_succeeded")
        issues.append(_issue(
            DiagnosticCode.POSTCONDITION_UNVERIFIED, DiagnosticSeverity.ERROR, "postcondition",
            "The action returned success, but no postcondition verification was recorded.",
            "Inspect the returned state and audit trail; do not treat the action response alone as proof of completion.",
            step_id, branch_id, _event_evidence(event),
        ))

    outcome = _step_outcome(events, issues)
    if outcome == StepOutcome.UNKNOWN and not any(
            issue.code == DiagnosticCode.STEP_OUTCOME_UNVERIFIED for issue in issues):
        issues.append(_issue(
            DiagnosticCode.STEP_OUTCOME_UNVERIFIED, DiagnosticSeverity.WARNING, "evidence",
            "The audit trail does not establish a final outcome for this step.",
            "Inspect the complete audit trail and the step handler's returned result before treating it as complete.",
            step_id, branch_id, {},
        ))
    return StepDiagnosis(step_id, branch_id, action, outcome, attempts, tuple(issues))


def _step_outcome(events: Sequence[Any],
                  issues: Sequence[WorkflowIssue]) -> StepOutcome:
    terminals = [event for event in events if _event_name(event) in (
        "step_completed", "step_failed", "step_skipped", "step_cancelled")]
    if terminals:
        name = _event_name(terminals[-1])
        if name == "step_completed":
            return StepOutcome.COMPLETED
        if name == "step_failed":
            return StepOutcome.FAILED
        if name == "step_skipped":
            return StepOutcome.SKIPPED
        return StepOutcome.CANCELLED

    for event in reversed(events):
        name = _event_name(event)
        details = _details(event)
        if name == "scheduled":
            return StepOutcome.SCHEDULED
        if name == "postcondition_verified":
            status = _status_value(details.get("status", "indeterminate"))
            if status == "pass":
                event_index = _event_index(events, event)
                action_passed = any(
                    _event_name(candidate) == "action_succeeded"
                    and _event_index(events, candidate) < event_index
                    for candidate in events
                )
                return StepOutcome.COMPLETED if action_passed else StepOutcome.UNKNOWN
            return StepOutcome.FAILED if status == "fail" else StepOutcome.INDETERMINATE
        if name == "precondition_verified":
            status = _status_value(details.get("status", "indeterminate"))
            if status == "fail":
                return StepOutcome.FAILED
            if status == "indeterminate":
                return StepOutcome.BLOCKED
            return StepOutcome.UNKNOWN
        if name == "action_failed":
            return StepOutcome.FAILED
        if name == "action_denied" or name == "dispatch_blocked":
            return StepOutcome.BLOCKED
        if name == "action_indeterminate":
            return StepOutcome.INDETERMINATE
        if name == "action_succeeded":
            return StepOutcome.INDETERMINATE

    codes = {issue.code for issue in issues}
    if DiagnosticCode.DEPENDENCY_NOT_SATISFIED in codes:
        return StepOutcome.SKIPPED
    return StepOutcome.UNKNOWN


def _verification_issue(code: DiagnosticCode, stage: str, step_id: str,
                        branch_id: str, event: Any,
                        severity: DiagnosticSeverity = DiagnosticSeverity.ERROR) -> WorkflowIssue:
    details = _details(event)
    status = _status_value(details.get("status", "indeterminate"))
    message = _verification_message(stage, status, details)
    if code == DiagnosticCode.PRECONDITION_FAILED:
        recommendation = "Compare the failed observations with their expected values; correct or refresh them before rerunning."
    elif code == DiagnosticCode.PRECONDITION_INDETERMINATE:
        recommendation = "Check whether required observations are missing, stale, invalid, or using the wrong unit."
    elif code == DiagnosticCode.POSTCONDITION_FAILED:
        recommendation = "Inspect the action output and actual state; the required outcome was not verified."
    else:
        recommendation = "Check whether required post-action observations are missing, stale, invalid, or using the wrong unit."
    return _issue(code, severity, stage, message, recommendation,
                  step_id, branch_id, _event_evidence(event))


def _verification_message(stage: str, status: str, details: Mapping[str, Any]) -> str:
    findings = details.get("findings", ())
    failed = [finding for finding in findings
              if isinstance(finding, Mapping)
              and _status_value(finding.get("status", "pass")) != "pass"]
    parts = []
    for finding in failed:
        label = str(finding.get("constraint") or "constraint")
        finding_status = _status_value(finding.get("status", "unknown"))
        message = finding.get("message")
        item = "%s (%s)" % (label, finding_status)
        if message:
            item += ": %s" % message
        values = []
        if "observed" in finding:
            values.append("observed=%r" % (finding.get("observed"),))
        if "expected" in finding:
            values.append("expected=%r" % (finding.get("expected"),))
        if values:
            item += " [" + ", ".join(values) + "]"
        parts.append(item)
    if parts:
        return "%s verification %s: %s" % (stage, status, "; ".join(parts))
    return "%s verification is %s." % (stage, status)


def _run_issue(event_name: str, step_id: Optional[str], branch_id: str,
               event: Any) -> WorkflowIssue:
    details = _details(event)
    if event_name == "run_timeout":
        return _issue(
            DiagnosticCode.RUN_TIMEOUT, DiagnosticSeverity.ERROR, "workflow",
            "The workflow deadline expired before the run completed.",
            "Inspect the active step and deadline; reconcile external side effects before retrying.",
            step_id, branch_id, _event_evidence(event),
        )
    return _issue(
        DiagnosticCode.RUN_CANCELLED, DiagnosticSeverity.WARNING, "workflow",
        "The workflow was cancelled: %s." % str(details.get("reason", "cancellation requested")),
        "Check why cancellation was requested and reconcile any action that was still in flight.",
        step_id, branch_id, _event_evidence(event),
    )


def _event_evidence(event: Any) -> Mapping[str, Any]:
    name = _event_name(event)
    details = _details(event)
    allowed = {
        "precondition_verified": ("status", "policy", "version", "findings"),
        "postcondition_verified": ("status", "policy", "version", "findings"),
        "action_succeeded": ("action", "message", "attempt", "elapsed_seconds"),
        "action_failed": ("action", "message", "attempt", "elapsed_seconds"),
        "action_denied": ("action", "message", "attempt", "elapsed_seconds"),
        "action_indeterminate": ("action", "message", "attempt", "elapsed_seconds"),
        "dispatch_blocked": ("reason",),
        "step_skipped": ("reason", "dependency", "dependency_status"),
        "step_failed": ("reason",),
        "step_cancelled": ("reason",),
        "run_cancelled": ("reason",),
        "run_timeout": ("reason", "deadline"),
        "scheduled": ("not_before",),
        "branch_transition": ("from", "to", "via"),
    }.get(name, ())
    selected = {key: details[key] for key in allowed if key in details}
    return {
        "event": name,
        "timestamp": getattr(event, "timestamp", None),
        "sequence_number": getattr(event, "sequence_number", 0),
        "branch_id": getattr(event, "branch_id", "default"),
        "details": _json_safe(selected),
    }


def _issue(code: DiagnosticCode, severity: DiagnosticSeverity, stage: str,
           message: str, recommendation: str, step_id: Optional[str],
           branch_id: Optional[str], evidence: Mapping[str, Any]) -> WorkflowIssue:
    return WorkflowIssue(code, severity, stage, message, recommendation,
                         step_id, branch_id, _json_safe(evidence))


def _details(event: Any) -> Mapping[str, Any]:
    value = getattr(event, "details", {})
    return value if isinstance(value, Mapping) else {}


def _event_name(event: Any) -> str:
    return str(getattr(event, "event", ""))


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def _event_index(events: Sequence[Any], target: Any) -> int:
    for index, event in enumerate(events):
        if event is target:
            return index
    return -1


def _summary(run_status: str, step_count: int,
             issues: Sequence[WorkflowIssue]) -> str:
    issue_count = len(issues)
    if run_status == RunStatus.COMPLETED.value:
        if issue_count:
            return "Workflow completed with %d issue%s to review across %d observed step%s." % (
                issue_count, "" if issue_count == 1 else "s",
                step_count, "" if step_count == 1 else "s")
        return "Workflow completed; all %d observed step%s passed verification." % (
            step_count, "" if step_count == 1 else "s")
    if run_status == RunStatus.SCHEDULED.value:
        return "Workflow is scheduled; no completion has been verified yet."
    if issue_count:
        return "Workflow %s with %d issue%s across %d observed step%s." % (
            run_status, issue_count, "" if issue_count == 1 else "s",
            step_count, "" if step_count == 1 else "s")
    return "Workflow %s; the audit trail contains no specific diagnostic issue." % run_status


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return repr(value)
