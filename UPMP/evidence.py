"""Evidence completeness validation for UPMP audit events.

M5 scope — provides schema-based validation of AuditEvent.details so
that an event with missing required fields is detectable. This closes
THREAT.md T-NEW: "an event's details may omit fields that the policy
expected."

Design:
  - EvidenceSchema is a mapping from event name to a set of required
    field names. The default schema (DEFAULT_EVIDENCE_SCHEMA) matches
    the orchestrator's current emission patterns (closed_loop.py).
  - EvidenceValidator validates AuditEvent objects against a schema and
    returns a list of EvidenceIssue records (empty list = valid).
  - validate_chain(events) validates a sequence and returns all issues.
  - The validator does NOT raise on invalid events; it returns issues.
    This matches the audit module's philosophy (malformed events don't
    crash the verifier). A strict variant can be added if needed.

Threat model notes (see THREAT.md):
  - Evidence completeness is a CORRECTNESS property, not a security
    control. It ensures the audit trail has the fields needed for
    forensic analysis; it does not authenticate the trail.
  - The schema is a plain dict; callers can customize it for domain-
    specific events. Unknown event types (not in the schema) are
    reported as issues by default, but this is configurable.
  - The validator checks field PRESENCE, not field TYPES. Type checking
    is a separate concern (and harder to do generically without
    coupling to the contracts module's dataclass shapes).

Backward compatibility:
  - Pure addition: no existing module imports this. The orchestrator's
    event emission is unchanged. Callers opt in by constructing an
    EvidenceValidator and calling validate() on emitted events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence

from .closed_loop import AuditEvent


# Default schema: maps event name to the set of required fields in
# AuditEvent.details. Derived from the orchestrator's emission patterns
# in UPMP/closed_loop.py (M2.2 state) + UPMP/parallel.py (M7):
#
#   scheduled              -> not_before
#   precondition_verified  -> status, policy, version, findings
#   dispatch_blocked       -> reason
#   action_succeeded        -> action, principal_ref, request_id, message,
#                              idempotency_key, attempt, elapsed_seconds
#   action_failed           -> (same as action_succeeded)
#   action_denied           -> (same as action_succeeded)
#   action_indeterminate    -> (same as action_succeeded)
#   postcondition_verified  -> status, policy, version, findings
#   run_cancelled           -> reason
#   run_timeout             -> reason, deadline
#   step_skipped            -> reason, dependency, dependency_status  (M7)
#   step_completed          -> (no required fields; terminal marker)     (M7)
#   step_failed             -> reason                                    (M7)
#   step_cancelled          -> reason                                    (M7)
DEFAULT_EVIDENCE_SCHEMA: Dict[str, FrozenSet[str]] = {
    "scheduled": frozenset({"not_before"}),
    "precondition_verified": frozenset({"status", "policy", "version", "findings"}),
    "dispatch_blocked": frozenset({"reason"}),
    "action_succeeded": frozenset({
        "action", "principal_ref", "request_id", "message",
        "idempotency_key", "attempt", "elapsed_seconds",
    }),
    "action_failed": frozenset({
        "action", "principal_ref", "request_id", "message",
        "idempotency_key", "attempt", "elapsed_seconds",
    }),
    "action_denied": frozenset({
        "action", "principal_ref", "request_id", "message",
        "idempotency_key", "attempt", "elapsed_seconds",
    }),
    "action_indeterminate": frozenset({
        "action", "principal_ref", "request_id", "message",
        "idempotency_key", "attempt", "elapsed_seconds",
    }),
    "postcondition_verified": frozenset({"status", "policy", "version", "findings"}),
    "run_cancelled": frozenset({"reason"}),
    "run_timeout": frozenset({"reason", "deadline"}),
    # M7 parallel-orchestrator events:
    "step_skipped": frozenset({"reason", "dependency", "dependency_status"}),
    "step_completed": frozenset(),
    "step_failed": frozenset({"reason"}),
    "step_cancelled": frozenset({"reason"}),
    # M7.1 transition event:
    "branch_transition": frozenset({"from", "to", "via"}),
}


@dataclass(frozen=True)
class EvidenceIssue:
    """A single evidence-completeness issue found during validation.

    Attributes:
        event_name: the AuditEvent.event field of the offending event.
        sequence_number: the event's sequence_number (0 if unchained).
        missing: required fields that are absent from details.
        unknown_event_type: True if the event name is not in the schema.
            When True, `missing` is empty (we can't know what fields an
            unknown event type should have).
    """
    event_name: str
    sequence_number: int
    missing: FrozenSet[str] = field(default_factory=frozenset)
    unknown_event_type: bool = False

    def __str__(self) -> str:
        if self.unknown_event_type:
            return ("event %r (seq=%d): unknown event type, no schema"
                    % (self.event_name, self.sequence_number))
        return ("event %r (seq=%d): missing required fields: %s"
                % (self.event_name, self.sequence_number, sorted(self.missing)))


class EvidenceValidator:
    """Validates AuditEvent objects against an EvidenceSchema.

    Default behavior:
      - Events with names in the schema are checked for required fields.
      - Events with names NOT in the schema are reported as issues
        (unknown_event_type=True) unless allow_unknown=True.
      - Returns a list of EvidenceIssue records (empty = valid).

    Usage:
        validator = EvidenceValidator()  # uses DEFAULT_EVIDENCE_SCHEMA
        issues = validator.validate(some_audit_event)
        if issues:
            print("evidence incomplete:", issues)

        # Validate a whole chain:
        issues = validator.validate_chain(workflow_result.audit)
    """

    def __init__(self,
                 schema: Optional[Mapping[str, FrozenSet[str]]] = None,
                 allow_unknown: bool = False) -> None:
        self.schema: Dict[str, FrozenSet[str]] = dict(schema) if schema is not None else dict(DEFAULT_EVIDENCE_SCHEMA)
        self.allow_unknown = allow_unknown

    def validate(self, event: AuditEvent) -> List[EvidenceIssue]:
        """Validate a single AuditEvent. Returns a list of issues
        (empty = valid). Never raises."""
        issues: List[EvidenceIssue] = []
        try:
            event_name = event.event
            seq = event.sequence_number
            details = event.details
        except AttributeError:
            # Malformed event object — can't validate.
            issues.append(EvidenceIssue(
                event_name="<malformed>", sequence_number=0,
                missing=frozenset(), unknown_event_type=True))
            return issues

        if event_name not in self.schema:
            if not self.allow_unknown:
                issues.append(EvidenceIssue(
                    event_name=event_name, sequence_number=seq,
                    unknown_event_type=True))
            return issues

        required = self.schema[event_name]
        try:
            present = set(details.keys()) if details else set()
        except AttributeError:
            # details is not a mapping
            issues.append(EvidenceIssue(
                event_name=event_name, sequence_number=seq,
                missing=frozenset(required), unknown_event_type=False))
            return issues

        missing = required - present
        if missing:
            issues.append(EvidenceIssue(
                event_name=event_name, sequence_number=seq,
                missing=frozenset(missing), unknown_event_type=False))
        return issues

    def validate_chain(self, events: Sequence[AuditEvent]) -> List[EvidenceIssue]:
        """Validate a sequence of AuditEvents. Returns all issues found
        across all events (in order). Never raises."""
        all_issues: List[EvidenceIssue] = []
        for event in events:
            all_issues.extend(self.validate(event))
        return all_issues

    def is_valid(self, event: AuditEvent) -> bool:
        """Convenience: True iff validate(event) returns no issues."""
        return not self.validate(event)

    def is_chain_valid(self, events: Sequence[AuditEvent]) -> bool:
        """Convenience: True iff validate_chain(events) returns no issues."""
        return not self.validate_chain(events)
