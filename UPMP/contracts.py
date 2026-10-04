"""Typed, domain-neutral contracts for observations and hard verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


class DataQuality(str, Enum):
    VALID = "valid"
    STALE = "stale"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class FindingStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class Observation:
    value: Any
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = "unspecified"
    unit: Optional[str] = None
    quality: DataQuality = DataQuality.VALID
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            parsed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp must include a timezone")
        except (AttributeError, ValueError) as exc:
            raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
        if not self.source:
            raise ValueError("observation source must be non-empty")
        if not isinstance(self.quality, DataQuality):
            object.__setattr__(self, "quality", DataQuality(self.quality))


@dataclass(frozen=True)
class ProcessState:
    schema_id: str
    schema_version: str
    observations: Mapping[str, Observation]
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    state_id: str = ""

    def __post_init__(self) -> None:
        if not self.schema_id or not self.schema_version:
            raise ValueError("schema_id and schema_version are required")
        try:
            parsed = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp must include a timezone")
        except (AttributeError, ValueError) as exc:
            raise ValueError("captured_at must be an ISO-8601 timestamp") from exc
        if any(not isinstance(v, Observation) for v in self.observations.values()):
            raise TypeError("all state values must be Observation instances")


@dataclass(frozen=True)
class Constraint:
    """A hard predicate. Missing/poor-quality observations never pass."""
    field: str
    operator: str
    expected: Any = None
    unit: Optional[str] = None
    max_age_seconds: Optional[float] = None
    name: str = ""
    description: str = ""

    OPERATORS = frozenset({"exists", "eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in"})

    def __post_init__(self) -> None:
        if not self.field:
            raise ValueError("constraint field must be non-empty")
        if self.operator not in self.OPERATORS:
            raise ValueError("unsupported constraint operator: %s" % self.operator)
        if self.max_age_seconds is not None and self.max_age_seconds < 0:
            raise ValueError("max_age_seconds cannot be negative")


@dataclass(frozen=True)
class VerificationFinding:
    constraint: str
    status: FindingStatus
    message: str
    observed: Any = None
    expected: Any = None
    severity: str = "error"
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationResult:
    status: FindingStatus
    findings: Sequence[VerificationFinding]
    policy_id: str
    policy_version: str
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class VerificationPolicy:
    policy_id: str
    version: str
    constraints: Sequence[Constraint] = ()

    def __post_init__(self) -> None:
        if not self.policy_id or not self.version:
            raise ValueError("policy_id and version are required")

    def evaluate(self, state: ProcessState, now: Optional[datetime] = None) -> VerificationResult:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must include a timezone")
        findings: List[VerificationFinding] = []
        if not self.constraints:
            finding = VerificationFinding(
                "policy configuration", FindingStatus.INDETERMINATE,
                "policy has no constraints; verification cannot authorize progress",
            )
            return VerificationResult(FindingStatus.INDETERMINATE, (finding,), self.policy_id, self.version)
        for constraint in self.constraints:
            findings.append(_evaluate_constraint(constraint, state, now))
        if any(f.status == FindingStatus.FAIL for f in findings):
            status = FindingStatus.FAIL
        elif any(f.status == FindingStatus.INDETERMINATE for f in findings):
            status = FindingStatus.INDETERMINATE
        else:
            status = FindingStatus.PASS
        return VerificationResult(status, tuple(findings), self.policy_id, self.version)


def _evaluate_constraint(c: Constraint, state: ProcessState, now: datetime) -> VerificationFinding:
    name = c.name or "%s %s %r" % (c.field, c.operator, c.expected)
    obs = state.observations.get(c.field)
    if c.operator == "exists" and obs is None:
        return VerificationFinding(name, FindingStatus.FAIL, "required observation is missing", expected="present")
    if obs is None:
        return VerificationFinding(name, FindingStatus.INDETERMINATE, "observation is missing", expected=c.expected)
    evidence = {"source": obs.source, "observed_at": obs.observed_at, "unit": obs.unit, "quality": obs.quality.value}
    if obs.quality != DataQuality.VALID:
        return VerificationFinding(name, FindingStatus.INDETERMINATE, "observation quality is %s" % obs.quality.value, obs.value, c.expected, evidence=evidence)
    if c.unit is not None and obs.unit != c.unit:
        return VerificationFinding(name, FindingStatus.INDETERMINATE, "unit mismatch", obs.value, c.expected, evidence=evidence)
    stamp = datetime.fromisoformat(obs.observed_at.replace("Z", "+00:00"))
    age_seconds = (now - stamp).total_seconds()
    if age_seconds < -5:
        return VerificationFinding(name, FindingStatus.INDETERMINATE, "observation timestamp is in the future", obs.value, c.expected, evidence=evidence)
    if c.max_age_seconds is not None and age_seconds > c.max_age_seconds:
        return VerificationFinding(name, FindingStatus.INDETERMINATE, "observation is stale", obs.value, c.expected, evidence=evidence)
    if c.operator == "exists":
        passed = True
    else:
        try:
            passed = {
                "eq": lambda: obs.value == c.expected,
                "ne": lambda: obs.value != c.expected,
                "lt": lambda: obs.value < c.expected,
                "lte": lambda: obs.value <= c.expected,
                "gt": lambda: obs.value > c.expected,
                "gte": lambda: obs.value >= c.expected,
                "in": lambda: obs.value in c.expected,
                "not_in": lambda: obs.value not in c.expected,
            }[c.operator]()
        except (TypeError, ValueError, KeyError):
            return VerificationFinding(name, FindingStatus.INDETERMINATE, "values cannot be compared", obs.value, c.expected, evidence=evidence)
    return VerificationFinding(name, FindingStatus.PASS if passed else FindingStatus.FAIL,
                               "constraint satisfied" if passed else "constraint violated",
                               obs.value, c.expected, evidence=evidence)

