"""Typed, domain-neutral contracts for observations and hard verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


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
class FieldSchema:
    """Machine-readable contract for one observation field.

    ``value_type`` uses JSON Schema's basic type names. Units are labels only:
    adapters remain responsible for converting values before state creation.
    """
    value_type: str
    required: bool = True
    unit: Optional[str] = None
    enum: Sequence[Any] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    VALUE_TYPES = frozenset({"null", "boolean", "integer", "number", "string", "array", "object"})

    def __post_init__(self) -> None:
        if self.value_type not in self.VALUE_TYPES:
            raise ValueError("unsupported schema value_type: %s" % self.value_type)
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("field minimum cannot exceed maximum")


@dataclass(frozen=True)
class StateSchema:
    """Versioned state schema suitable for serialization by domain adapters."""
    schema_id: str
    version: str
    fields: Mapping[str, FieldSchema]
    allow_additional_fields: bool = False

    def __post_init__(self) -> None:
        if not self.schema_id or not self.version:
            raise ValueError("schema_id and version are required")
        if any(not key for key in self.fields):
            raise ValueError("schema field names must be non-empty")
        if any(not isinstance(spec, FieldSchema) for spec in self.fields.values()):
            raise TypeError("schema fields must be FieldSchema instances")


@dataclass(frozen=True)
class SchemaValidationResult:
    valid: bool
    schema_id: str
    schema_version: str
    errors: Sequence[str] = ()


class SchemaRegistry:
    """Validates states against explicitly registered schema versions."""

    def __init__(self, schemas: Sequence[StateSchema] = ()) -> None:
        self._schemas: Dict[Tuple[str, str], StateSchema] = {}
        for schema in schemas:
            self.register(schema)

    def register(self, schema: StateSchema) -> None:
        key = (schema.schema_id, schema.version)
        if key in self._schemas:
            raise ValueError("schema version already registered: %s@%s" % key)
        self._schemas[key] = schema

    def get(self, schema_id: str, version: str) -> Optional[StateSchema]:
        return self._schemas.get((schema_id, version))

    def validate(self, state: ProcessState) -> SchemaValidationResult:
        schema = self.get(state.schema_id, state.schema_version)
        if schema is None:
            return SchemaValidationResult(False, state.schema_id, state.schema_version,
                                          ("schema version is not registered",))
        errors: List[str] = []
        for name, spec in schema.fields.items():
            observation = state.observations.get(name)
            if observation is None:
                if spec.required:
                    errors.append("required observation is missing: %s" % name)
                continue
            if not _matches_type(observation.value, spec.value_type):
                errors.append("%s must have type %s" % (name, spec.value_type))
                continue
            if spec.unit is not None and observation.unit != spec.unit:
                errors.append("%s must use unit %s" % (name, spec.unit))
            if spec.enum and observation.value not in spec.enum:
                errors.append("%s must be one of the declared values" % name)
            if isinstance(observation.value, (int, float)) and not isinstance(observation.value, bool):
                if spec.minimum is not None and observation.value < spec.minimum:
                    errors.append("%s is below minimum %s" % (name, spec.minimum))
                if spec.maximum is not None and observation.value > spec.maximum:
                    errors.append("%s is above maximum %s" % (name, spec.maximum))
        if not schema.allow_additional_fields:
            for name in state.observations:
                if name not in schema.fields:
                    errors.append("undeclared observation: %s" % name)
        return SchemaValidationResult(not errors, schema.schema_id, schema.version, tuple(errors))


def _matches_type(value: Any, value_type: str) -> bool:
    if value_type == "null":
        return value is None
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "array":
        return isinstance(value, (list, tuple))
    if value_type == "object":
        return isinstance(value, Mapping)
    return False


@dataclass(frozen=True)
class Constraint:
    """A hard predicate. Missing/poor-quality observations never pass."""
    field: str
    operator: str
    expected: Any = None
    unit: Optional[str] = None
    max_age_seconds: Optional[float] = None
    tolerance: Optional[float] = None
    name: str = ""
    description: str = ""

    OPERATORS = frozenset({"exists", "eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "between", "approx"})

    def __post_init__(self) -> None:
        if not self.field:
            raise ValueError("constraint field must be non-empty")
        if self.operator not in self.OPERATORS:
            raise ValueError("unsupported constraint operator: %s" % self.operator)
        if self.max_age_seconds is not None and self.max_age_seconds < 0:
            raise ValueError("max_age_seconds cannot be negative")
        if self.tolerance is not None and self.tolerance < 0:
            raise ValueError("tolerance cannot be negative")
        if self.operator == "between" and (not isinstance(self.expected, (tuple, list)) or len(self.expected) != 2):
            raise ValueError("between expects a two-item (minimum, maximum) range")
        if self.operator == "approx" and self.tolerance is None:
            raise ValueError("approx requires an explicit tolerance")


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
                "between": lambda: c.expected[0] <= obs.value <= c.expected[1],
                "approx": lambda: abs(obs.value - c.expected) <= c.tolerance,
            }[c.operator]()
        except (TypeError, ValueError, KeyError):
            return VerificationFinding(name, FindingStatus.INDETERMINATE, "values cannot be compared", obs.value, c.expected, evidence=evidence)
    return VerificationFinding(name, FindingStatus.PASS if passed else FindingStatus.FAIL,
                               "constraint satisfied" if passed else "constraint violated",
                               obs.value, c.expected, evidence=evidence)

