"""Chartered, evidence-based run snapshots for the UPMP River Map.

The observatory view deliberately separates workflow termination from goal
verification. Progress is counted from passing precondition and postcondition
checkpoints; heuristic monitor scores are not used as completion evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .closed_loop import AuditEvent, RunStatus, WorkflowResult, WorkflowStep
from .contracts import Constraint, FindingStatus, ProcessState, VerificationPolicy
from .flow_graph import workflow_flow_graph


class GoalOutcome(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class CheckpointStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"
    NOT_VERIFIED = "not_verified"
    PENDING = "pending"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ProcessPart:
    """A group or step in the declared process hierarchy.

    Group parts have child parts. Leaf parts name exactly one workflow step.
    Optional weights are relative to siblings and are used only when every
    required sibling has an explicit weight.
    """

    part_id: str
    label: str
    parent_id: Optional[str] = None
    step_id: Optional[str] = None
    required: bool = True
    weight: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.part_id or not self.label:
            raise ValueError("part_id and label are required")
        if self.parent_id == self.part_id:
            raise ValueError("a process part cannot parent itself")
        if self.step_id is not None and not self.step_id:
            raise ValueError("step_id must be non-empty when supplied")
        if self.weight is not None and (
            not math.isfinite(float(self.weight)) or float(self.weight) <= 0
        ):
            raise ValueError("part weight must be a finite positive number")


def _freeze_json(value: Any) -> Any:
    """Copy JSON-like policy values into recursively immutable containers."""
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("charter policy values must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_json(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError("charter policy values must be JSON-compatible")


def _thaw_json(value: Any) -> Any:
    """Return normal JSON containers for contract evaluation."""
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


@dataclass(frozen=True)
class ProcessCharter:
    """Immutable, versioned goal contract bound to a process run."""

    process_id: str
    version: str
    goal: str
    acceptance_policy: VerificationPolicy
    parts: Tuple[ProcessPart, ...] = ()
    map_version: str = "1"

    def __post_init__(self) -> None:
        if not self.process_id or not self.version or not self.goal:
            raise ValueError("process_id, version, and goal are required")
        if not self.map_version:
            raise ValueError("map_version is required")
        if not isinstance(self.acceptance_policy, VerificationPolicy):
            raise TypeError("acceptance_policy must be a VerificationPolicy")

        frozen_constraints = tuple(
            Constraint(
                field=constraint.field,
                operator=constraint.operator,
                expected=_freeze_json(constraint.expected),
                unit=constraint.unit,
                max_age_seconds=constraint.max_age_seconds,
                tolerance=constraint.tolerance,
                name=constraint.name,
                description=constraint.description,
            )
            for constraint in self.acceptance_policy.constraints
        )
        object.__setattr__(
            self,
            "acceptance_policy",
            VerificationPolicy(
                self.acceptance_policy.policy_id,
                self.acceptance_policy.version,
                frozen_constraints,
            ),
        )
        parts = tuple(self.parts)
        object.__setattr__(self, "parts", parts)
        self._validate_parts(parts)
        # Validate that all policy values have a stable JSON representation.
        json.dumps(self._payload(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _validate_parts(parts: Sequence[ProcessPart]) -> None:
        ids = [part.part_id for part in parts]
        if len(ids) != len(set(ids)):
            raise ValueError("process part IDs must be unique")
        known = set(ids)
        for part in parts:
            if part.parent_id is not None and part.parent_id not in known:
                raise ValueError("unknown parent process part: %s" % part.parent_id)
        children: Dict[str, List[str]] = defaultdict(list)
        step_ids = []
        for part in parts:
            if part.parent_id:
                children[part.parent_id].append(part.part_id)
            if part.step_id is not None:
                step_ids.append(part.step_id)
                if children.get(part.part_id):
                    raise ValueError("workflow step parts must be leaves")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("each workflow step must map to one process part")
        for part in parts:
            if part.step_id is not None and children.get(part.part_id):
                raise ValueError("workflow step parts must be leaves")
            if part.step_id is None and not children.get(part.part_id):
                raise ValueError("process groups must contain child parts")
        for part in parts:
            seen = set()
            cursor = part
            while cursor.parent_id is not None:
                if cursor.part_id in seen:
                    raise ValueError("process part hierarchy cannot contain cycles")
                seen.add(cursor.part_id)
                cursor = next(item for item in parts if item.part_id == cursor.parent_id)

    def _payload(self) -> Mapping[str, Any]:
        return {
            "process_id": self.process_id,
            "version": self.version,
            "goal": self.goal,
            "map_version": self.map_version,
            "acceptance_policy": _jsonable(self.acceptance_policy),
            "parts": [_jsonable(part) for part in self.parts],
        }

    @property
    def charter_hash(self) -> str:
        canonical = json.dumps(
            self._payload(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def audit_metadata(self) -> Mapping[str, str]:
        return {
            "process_id": self.process_id,
            "charter_version": self.version,
            "charter_hash": self.charter_hash,
        }

    def to_dict(self) -> Mapping[str, Any]:
        return {
            **self._payload(),
            "charter_hash": self.charter_hash,
        }


@dataclass(frozen=True)
class CheckpointView:
    checkpoint_id: str
    step_id: str
    phase: str
    status: CheckpointStatus
    policy_id: Optional[str] = None
    policy_version: Optional[str] = None
    findings: Tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "step_id": self.step_id,
            "phase": self.phase,
            "status": self.status.value,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "findings": [_jsonable(item) for item in self.findings],
        }


@dataclass(frozen=True)
class StepRunView:
    step_id: str
    status: str
    attempts: int
    passed_checkpoints: int
    total_checkpoints: int
    checkpoints: Tuple[CheckpointView, ...]

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "attempts": self.attempts,
            "passed_checkpoints": self.passed_checkpoints,
            "total_checkpoints": self.total_checkpoints,
            "checkpoints": [item.to_dict() for item in self.checkpoints],
        }


@dataclass(frozen=True)
class PartRunView:
    part_id: str
    label: str
    parent_id: Optional[str]
    step_id: Optional[str]
    required: bool
    weight: Optional[float]
    status: str
    passed_checkpoints: int
    total_checkpoints: int
    progress_percent: Optional[float]
    progress_method: str
    child_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "part_id": self.part_id,
            "label": self.label,
            "parent_id": self.parent_id,
            "step_id": self.step_id,
            "required": self.required,
            "weight": self.weight,
            "status": self.status,
            "passed_checkpoints": self.passed_checkpoints,
            "total_checkpoints": self.total_checkpoints,
            "progress_percent": self.progress_percent,
            "progress_method": self.progress_method,
            "child_ids": list(self.child_ids),
        }


@dataclass(frozen=True)
class ProcessRunView:
    run_id: Optional[str]
    process_id: str
    charter_version: str
    charter_hash: str
    charter_bound: bool
    run_status: str
    goal_outcome: GoalOutcome
    goal_verification: Mapping[str, Any]
    passed_checkpoints: int
    total_checkpoints: int
    progress_percent: Optional[float]
    progress_method: str
    steps: Tuple[StepRunView, ...]
    parts: Tuple[PartRunView, ...]
    flow: Mapping[str, Any]
    events: Tuple[Mapping[str, Any], ...] = ()
    deviations: Tuple[Mapping[str, Any], ...] = ()
    mutations: Tuple[Mapping[str, Any], ...] = ()
    goal: str = ""
    acceptance_policy_id: str = ""
    acceptance_policy_version: str = ""

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "schema_version": "1",
            "run": {
                "run_id": self.run_id,
                "process_id": self.process_id,
                "charter_version": self.charter_version,
                "charter_hash": self.charter_hash,
                "charter_bound": self.charter_bound,
                "run_status": self.run_status,
                "goal_outcome": self.goal_outcome.value,
                "goal_verification": _jsonable(self.goal_verification),
            },
            "charter": {
                "goal": self.goal,
                "acceptance_policy_id": self.acceptance_policy_id,
                "acceptance_policy_version": self.acceptance_policy_version,
            },
            "progress": {
                "passed_checkpoints": self.passed_checkpoints,
                "total_checkpoints": self.total_checkpoints,
                "percent": self.progress_percent,
                "method": self.progress_method,
            },
            "steps": [item.to_dict() for item in self.steps],
            "parts": [item.to_dict() for item in self.parts],
            "flow": _jsonable(self.flow),
            "events": [_jsonable(item) for item in self.events],
            "deviations": [_jsonable(item) for item in self.deviations],
            "mutations": [_jsonable(item) for item in self.mutations],
        }


def _checkpoint(step_id: str, phase: str, event: Any, fallback: CheckpointStatus) -> CheckpointView:
    if event is None:
        return CheckpointView("%s:%s" % (step_id, phase), step_id, phase, fallback)
    details = getattr(event, "details", {}) or {}
    status = {
        FindingStatus.PASS.value: CheckpointStatus.PASSED,
        FindingStatus.FAIL.value: CheckpointStatus.FAILED,
        FindingStatus.INDETERMINATE.value: CheckpointStatus.INDETERMINATE,
    }.get(str(details.get("status", "")), CheckpointStatus.INDETERMINATE)
    return CheckpointView(
        "%s:%s" % (step_id, phase), step_id, phase, status,
        str(details.get("policy")) if details.get("policy") is not None else None,
        str(details.get("version")) if details.get("version") is not None else None,
        tuple(_jsonable(item) for item in details.get("findings", ())),
    )


def _step_status(events: Sequence[Any]) -> str:
    for event in reversed(events):
        name = str(getattr(event, "event", ""))
        details = getattr(event, "details", {}) or {}
        if name == "postcondition_verified":
            return {
                "pass": "succeeded",
                "fail": "failed",
                "indeterminate": "indeterminate",
            }.get(str(details.get("status", "")), "indeterminate")
        if name == "step_completed":
            return "succeeded"
        if name == "step_failed" or name == "action_failed":
            return "failed"
        if name == "step_skipped":
            return "skipped"
        if name == "step_cancelled":
            return "cancelled"
        if name == "action_denied":
            return "blocked"
        if name == "action_indeterminate":
            return "indeterminate"
        if name == "dispatch_blocked":
            return "blocked" if details.get("reason") == "indeterminate" else "failed"
        if name == "precondition_verified":
            return {
                "pass": "waiting",
                "fail": "failed",
                "indeterminate": "blocked",
            }.get(str(details.get("status", "")), "indeterminate")
        if name == "action_succeeded":
            return "verifying"
        if name in ("run_cancelled", "run_timeout"):
            return "cancelled" if name == "run_cancelled" else "indeterminate"
        if name == "scheduled":
            return "waiting"
    return "waiting"


def _part_state(child_statuses: Sequence[str]) -> str:
    if not child_statuses:
        return "indeterminate"
    if "failed" in child_statuses:
        return "failed"
    if any(status in ("blocked", "indeterminate", "cancelled", "skipped") for status in child_statuses):
        return "indeterminate"
    if any(status in ("running", "verifying") for status in child_statuses):
        return "running"
    if "waiting" in child_statuses:
        return "waiting"
    return "succeeded"


def build_process_run_view(
    charter: ProcessCharter,
    steps: Sequence[WorkflowStep],
    result: WorkflowResult,
    now: Optional[datetime] = None,
    active_run: bool = False,
) -> ProcessRunView:
    """Build an honest, JSON-ready view from a completed workflow result.

    Each workflow step contributes two declared checkpoints: its precondition
    and postcondition policy. Only passing policy results count as progress.
    A completed run is not a verified goal unless the final charter policy
    passes and every event carries the same charter identity and hash.
    """
    if now is not None and now.tzinfo is None:
        raise ValueError("now must include a timezone")
    steps = tuple(steps)
    step_by_id = {step.step_id: step for step in steps}
    if len(step_by_id) != len(steps):
        raise ValueError("workflow step IDs must be unique")

    parts = charter.parts or tuple(
        ProcessPart(step.step_id, step.description or step.step_id, step_id=step.step_id)
        for step in steps
    )
    mapped_steps = {part.step_id for part in parts if part.step_id is not None}
    if mapped_steps != set(step_by_id):
        missing = sorted(set(step_by_id) - mapped_steps)
        extra = sorted(mapped_steps - set(step_by_id))
        raise ValueError("charter parts must map every workflow step exactly; missing=%s extra=%s" % (missing, extra))

    events_by_step: Dict[str, List[Any]] = defaultdict(list)
    run_ids = set()
    required_metadata = dict(charter.audit_metadata)
    charter_bound = bool(result.audit)
    for event in result.audit:
        details = getattr(event, "details", {}) or {}
        if details.get("run_id"):
            run_ids.add(str(details["run_id"]))
        if any(details.get(key) != value for key, value in required_metadata.items()):
            charter_bound = False
        if details.get("run_id") is None:
            charter_bound = False
        if event.step_id in step_by_id:
            events_by_step[event.step_id].append(event)
    if len(run_ids) != 1:
        charter_bound = False
    run_id = next(iter(run_ids)) if len(run_ids) == 1 else None

    step_views = []
    step_by_view: Dict[str, StepRunView] = {}
    for step in steps:
        step_events = events_by_step.get(step.step_id, ())
        pre_event = next((event for event in reversed(step_events) if event.event == "precondition_verified"), None)
        post_event = next((event for event in reversed(step_events) if event.event == "postcondition_verified"), None)
        step_state = _step_status(step_events)
        terminal = next((event for event in reversed(step_events) if event.event in {
            "step_skipped", "step_cancelled", "action_failed", "action_denied", "action_indeterminate",
        }), None)
        pre_fallback = CheckpointStatus.PENDING
        post_fallback = CheckpointStatus.PENDING
        if terminal is not None:
            if terminal.event == "step_skipped":
                pre_fallback = post_fallback = CheckpointStatus.SKIPPED
            elif post_event is None:
                post_fallback = CheckpointStatus.NOT_VERIFIED
        checkpoints = (
            _checkpoint(step.step_id, "precondition", pre_event, pre_fallback),
            _checkpoint(step.step_id, "postcondition", post_event, post_fallback),
        )
        attempts = sum(
            1 for event in step_events
            if str(getattr(event, "event", "")).startswith("action_")
        )
        view = StepRunView(
            step.step_id,
            step_state,
            attempts,
            sum(item.status == CheckpointStatus.PASSED for item in checkpoints),
            len(checkpoints),
            checkpoints,
        )
        step_views.append(view)
        step_by_view[step.step_id] = view

    part_by_id = {part.part_id: part for part in parts}
    children: Dict[Optional[str], List[ProcessPart]] = defaultdict(list)
    for part in parts:
        children[part.parent_id].append(part)
    part_views: Dict[str, PartRunView] = {}

    def build_part(part: ProcessPart, ancestor_required: bool = True) -> PartRunView:
        required = ancestor_required and part.required
        child_parts = children.get(part.part_id, ())
        if part.step_id is not None:
            step_view = step_by_view[part.step_id]
            passed = step_view.passed_checkpoints if required else 0
            total = step_view.total_checkpoints if required else 0
            state = step_view.status
            percent = round(100.0 * passed / total, 1) if total else None
            method = "checkpoint_count" if total else "none"
            child_ids: Tuple[str, ...] = ()
        else:
            child_views = [build_part(child, required) for child in child_parts]
            required_children = [view for view in child_views if view.required]
            statuses = [view.status for view in required_children]
            state = _part_state(statuses)
            passed = sum(view.passed_checkpoints for view in required_children)
            total = sum(view.total_checkpoints for view in required_children)
            use_weights = bool(required_children) and all(view.weight is not None for view in required_children)
            if use_weights:
                denominator = sum(float(view.weight) for view in required_children)
                numerator = sum(float(view.weight) * (view.progress_percent or 0.0) for view in required_children)
                percent = round(numerator / denominator, 1) if denominator else None
                method = "declared_weights"
            else:
                percent = round(100.0 * passed / total, 1) if total else None
                method = "checkpoint_count" if total else "none"
            child_ids = tuple(child.part_id for child in child_parts)
        view = PartRunView(
            part.part_id, part.label, part.parent_id, part.step_id, required,
            part.weight, state, passed, total, percent, method, child_ids,
        )
        part_views[part.part_id] = view
        return view

    roots = children.get(None, ())
    root_views = [build_part(root) for root in roots]
    required_roots = [view for view in root_views if view.required]
    passed = sum(view.passed_checkpoints for view in required_roots)
    total = sum(view.total_checkpoints for view in required_roots)
    if required_roots and all(view.weight is not None for view in required_roots):
        weight_total = sum(float(view.weight) for view in required_roots)
        weighted_pass = sum(float(view.weight) * (view.progress_percent or 0.0) for view in required_roots)
        progress_percent = round(weighted_pass / weight_total, 1) if weight_total else None
        progress_method = "declared_weights"
    else:
        progress_percent = round(100.0 * passed / total, 1) if total else None
        progress_method = "checkpoint_count" if total else "none"

        evaluation_policy = VerificationPolicy(
            charter.acceptance_policy.policy_id,
            charter.acceptance_policy.version,
            tuple(
                Constraint(
                    field=item.field, operator=item.operator,
                    expected=_thaw_json(item.expected), unit=item.unit,
                    max_age_seconds=item.max_age_seconds,
                    tolerance=item.tolerance, name=item.name,
                    description=item.description,
                )
                for item in charter.acceptance_policy.constraints
            ),
        )
        verification = evaluation_policy.evaluate(result.final_state, now)
    run_status = "running" if active_run else result.status.value
    if active_run:
        goal_outcome = GoalOutcome.INDETERMINATE
        verification_status = FindingStatus.INDETERMINATE
        verification_findings: List[Any] = []
    elif charter_bound and verification.status == FindingStatus.FAIL:
        goal_outcome = GoalOutcome.FAILED
        verification_status = verification.status
        verification_findings = list(verification.findings)
    elif (
        result.status == RunStatus.COMPLETED
        and verification.status == FindingStatus.PASS
        and charter_bound
    ):
        goal_outcome = GoalOutcome.VERIFIED
        verification_status = verification.status
        verification_findings = list(verification.findings)
    else:
        goal_outcome = GoalOutcome.INDETERMINATE
        verification_status = verification.status
        verification_findings = list(verification.findings)

    flow = workflow_flow_graph(steps, result.audit).to_dict()
    verification_dict = {
        "status": verification_status.value,
        "policy_id": verification.policy_id,
        "policy_version": verification.policy_version,
        "findings": [_jsonable(finding) for finding in verification_findings],
    }
    event_views = tuple({
        "event": event.event,
        "step_id": event.step_id,
        "timestamp": event.timestamp,
        "branch_id": getattr(event, "branch_id", "default"),
        "details": _jsonable(getattr(event, "details", {}) or {}),
    } for event in result.audit)
    deviations = []
    for event in result.audit:
        name = str(getattr(event, "event", ""))
        details = getattr(event, "details", {}) or {}
        is_deviation = (
            (name in ("precondition_verified", "postcondition_verified")
             and details.get("status") in (FindingStatus.FAIL.value, FindingStatus.INDETERMINATE.value))
            or name in ("action_failed", "action_denied", "action_indeterminate",
                        "step_skipped", "deviation_detected")
        )
        if is_deviation:
            deviations.append({
                "deviation_id": "%s:%s:%s" % (event.step_id, name, event.timestamp),
                "step_id": event.step_id,
                "event": name,
                "timestamp": event.timestamp,
                "status": str(details.get("status", details.get("reason", "attention"))),
                "evidence": _jsonable(details.get("findings", details)),
            })
    mutations = []
    for event in result.audit:
        name = str(getattr(event, "event", ""))
        if name in ("mutation_detected", "mutation_applied", "process_mutated"):
            mutations.append({
                "mutation_id": str((getattr(event, "details", {}) or {}).get("mutation_id", "%s:%s" % (event.step_id, event.timestamp))),
                "step_id": event.step_id,
                "timestamp": event.timestamp,
                "details": _jsonable(getattr(event, "details", {}) or {}),
            })
    return ProcessRunView(
        run_id,
        charter.process_id,
        charter.version,
        charter.charter_hash,
        charter_bound,
        run_status,
        goal_outcome,
        verification_dict,
        passed,
        total,
        progress_percent,
        progress_method,
        tuple(step_views),
        tuple(part_views[part.part_id] for part in parts),
        flow,
        event_views,
        tuple(deviations),
        tuple(mutations),
        charter.goal,
        charter.acceptance_policy.policy_id,
        charter.acceptance_policy.version,
    )


def write_process_run_snapshot(view: ProcessRunView, path: Any) -> Path:
    """Atomically replace a JSON snapshot consumed by the live River Map."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", suffix=".tmp",
            prefix=".%s." % target.name, dir=str(target.parent), delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(view.to_dict(), stream, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
        os.replace(str(temporary), str(target))
        return target
    except Exception:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise


class ProcessRunTracker:
    """Audit sink that publishes checkpoint progress as a JSON run snapshot.

    Pass this object as ``audit_sink`` to an orchestrator, then call
    ``finish(result)`` after ``run`` returns. Each accepted event atomically
    updates the UI snapshot. Optional downstream audit sinks receive the
    same event and retain their own persistence or hash-chain guarantees.
    """

    def __init__(
        self,
        charter: ProcessCharter,
        steps: Sequence[WorkflowStep],
        initial_state: ProcessState,
        snapshot_path: Any,
        downstream_sinks: Sequence[Any] = (),
    ) -> None:
        self.charter = charter
        self.steps = tuple(steps)
        self.initial_state = initial_state
        self.snapshot_path = Path(snapshot_path).expanduser().resolve()
        self.downstream_sinks = tuple(downstream_sinks)
        for sink in self.downstream_sinks:
            if not callable(getattr(sink, "append", None)):
                raise TypeError("each downstream sink must provide append(event)")
        self._events: List[Any] = []
        self._lock = threading.RLock()
        self._view: Optional[ProcessRunView] = None

    @property
    def current_view(self) -> Optional[ProcessRunView]:
        with self._lock:
            return self._view

    def _partial_result(self) -> WorkflowResult:
        return WorkflowResult(
            status=RunStatus.SCHEDULED,
            final_state=self.initial_state,
            current_step=self._events[-1].step_id if self._events else None,
            verification=None,
            actions=(),
            audit=tuple(self._events),
        )

    def _publish(self, active: bool) -> ProcessRunView:
        view = build_process_run_view(
            self.charter, self.steps, self._partial_result(), active_run=active,
        )
        write_process_run_snapshot(view, self.snapshot_path)
        self._view = view
        return view

    def begin(self) -> ProcessRunView:
        """Publish an initial waiting snapshot before the orchestrator starts."""
        with self._lock:
            return self._publish(active=True)

    def append(self, event: Any) -> int:
        """Accept one runtime audit event and publish actual progress."""
        with self._lock:
            sequences = [sink.append(event) for sink in self.downstream_sinks]
            self._events.append(event)
            try:
                self._publish(active=True)
            except Exception:
                self._events.pop()
                raise
            return next((int(value) for value in reversed(sequences)
                         if isinstance(value, int) and not isinstance(value, bool)), len(self._events))

    def record_mutation(
        self, mutation_id: str, step_id: str, details: Mapping[str, Any],
    ) -> int:
        """Record an adapter-observed mutation in this run's event stream."""
        if not mutation_id:
            raise ValueError("mutation_id is required")
        return self._record_observation("mutation_detected", step_id, {
            **dict(details), "mutation_id": mutation_id,
        })

    def record_deviation(
        self, deviation_id: str, step_id: str, details: Mapping[str, Any],
    ) -> int:
        """Record an adapter-observed deviation with its evidence."""
        if not deviation_id:
            raise ValueError("deviation_id is required")
        return self._record_observation("deviation_detected", step_id, {
            **dict(details), "deviation_id": deviation_id,
        })

    def _record_observation(
        self, event_name: str, step_id: str, details: Mapping[str, Any],
    ) -> int:
        if step_id not in {step.step_id for step in self.steps}:
            raise ValueError("observed process change references an unknown workflow step")
        if not isinstance(details, Mapping):
            raise TypeError("observed process change details must be a mapping")
        try:
            json.dumps(details, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("observed process change details must be JSON-compatible") from exc
        with self._lock:
            enriched = {**details, **self.charter.audit_metadata}
            run_ids = {
                str((getattr(event, "details", {}) or {}).get("run_id"))
                for event in self._events
                if (getattr(event, "details", {}) or {}).get("run_id")
            }
            if len(run_ids) == 1:
                enriched["run_id"] = next(iter(run_ids))
            event = AuditEvent(
                event_name, step_id, datetime.now(timezone.utc).isoformat(), enriched,
            )
            return self.append(event)

    def finish(self, result: WorkflowResult) -> ProcessRunView:
        """Publish the terminal result after all workflow evidence is recorded."""
        with self._lock:
            tracked = iter(self._events)
            if not all(any(actual is expected for actual in tracked)
                       for expected in result.audit):
                raise ValueError("final result audit does not match the tracked event stream")
            tracked_result = replace(result, audit=tuple(self._events))
            view = build_process_run_view(self.charter, self.steps, tracked_result)
            write_process_run_snapshot(view, self.snapshot_path)
            self._view = view
            return view
