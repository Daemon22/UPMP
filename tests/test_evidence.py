"""M5 tests — evidence completeness validation.

Covers:
  - EvidenceValidator with DEFAULT_EVIDENCE_SCHEMA validates events
    emitted by the orchestrator (no issues on a clean run).
  - Missing required fields are detected per event type.
  - Unknown event types reported (unless allow_unknown=True).
  - Malformed event objects (no .event, no .details) don't crash.
  - Custom schema works.
  - validate_chain aggregates issues across events.
  - is_valid / is_chain_valid convenience methods.
  - End-to-end: a real orchestrator run produces evidence that passes
    validation; tampering with an event's details is detected.
"""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from UPMP import (
    ActionDispatcher, ActionResult, ActionStatus, ClosedLoopOrchestrator,
    Constraint, Observation, ProcessState, VerificationPolicy, WorkflowStep,
)
from UPMP.audit import InMemoryAuditSink
from UPMP.closed_loop import AuditEvent
from UPMP.evidence import (
    DEFAULT_EVIDENCE_SCHEMA,
    EvidenceIssue,
    EvidenceValidator,
)


def state(**values):
    obs = {k: v if isinstance(v, Observation) else Observation(v, source="test")
           for k, v in values.items()}
    obs.setdefault("_test_guard", Observation(True, source="fixture"))
    return ProcessState("test.domain", "1", obs)


def policy(name, *constraints):
    if not constraints:
        constraints = (Constraint("_test_guard", "eq", True),)
    return VerificationPolicy(name, "1", constraints)


def make_event(name, details, seq=0):
    return AuditEvent(name, "step-1", "2026-01-01T00:00:00+00:00", details, sequence_number=seq)


def verification_details(status="pass", policy_id="p", version="1"):
    return {
        "status": status,
        "policy": policy_id,
        "version": version,
        "findings": [],
    }


def action_details(status="succeeded", action="act", attempt=1):
    return {
        "action": action,
        "principal_ref": "operator-1",
        "request_id": "req-123",
        "message": "done",
        "idempotency_key": "key-1",
        "attempt": attempt,
        "elapsed_seconds": 0.001,
    }


class TestDefaultSchema:
    def test_schema_covers_all_orchestrator_event_types(self):
        """The default schema must cover every event type the
        orchestrators emit (sequential + parallel)."""
        expected = {
            "scheduled", "precondition_verified", "dispatch_blocked",
            "action_succeeded", "action_failed", "action_denied",
            "action_indeterminate", "postcondition_verified",
            "run_cancelled", "run_timeout",
            # M7 parallel orchestrator events:
            "step_skipped", "step_completed", "step_failed", "step_cancelled",
            # M7.1 transition event:
            "branch_transition",
        }
        assert set(DEFAULT_EVIDENCE_SCHEMA.keys()) == expected

    def test_action_events_have_same_required_fields(self):
        """All action_* events share the same field set (the orchestrator
        emits them with the same details dict)."""
        action_fields = DEFAULT_EVIDENCE_SCHEMA["action_succeeded"]
        for status in ("failed", "denied", "indeterminate"):
            assert DEFAULT_EVIDENCE_SCHEMA["action_%s" % status] == action_fields

    def test_every_schema_event_type_is_emitted_by_a_test(self):
        """Every event type in DEFAULT_EVIDENCE_SCHEMA must be exercised
        by at least one test in the suite. This prevents the schema from
        silently growing an entry for an event type that nothing ever
        emits (which would make the schema unfalsifiable).

        This test greps the test files for each event type name. If a new
        event type is added to the schema, a corresponding test must
        emit it (construct an AuditEvent with that event name and assert
        the validator accepts it) — otherwise this test fails."""
        import os
        import glob

        test_dir = os.path.dirname(os.path.abspath(__file__))
        all_test_text = ""
        for path in glob.glob(os.path.join(test_dir, "test_*.py")):
            with open(path, "r", encoding="utf-8") as fh:
                all_test_text += fh.read()

        unexercised = []
        for event_type in DEFAULT_EVIDENCE_SCHEMA:
            # The event type must appear as a string literal in some test
            # file (either in a make_event("...") call, an AuditEvent
            # construction, or an assertion checking e.event == "...").
            if '"%s"' % event_type not in all_test_text:
                unexercised.append(event_type)

        assert unexercised == [], (
            "DEFAULT_EVIDENCE_SCHEMA contains event types not exercised by "
            "any test: %s. Either add a test that emits the event, or "
            "remove it from the schema." % unexercised
        )


class TestValidatorValidEvents:
    def test_valid_precondition_verified(self):
        v = EvidenceValidator()
        e = make_event("precondition_verified", verification_details(), seq=1)
        assert v.validate(e) == []
        assert v.is_valid(e) is True

    def test_valid_action_succeeded(self):
        v = EvidenceValidator()
        e = make_event("action_succeeded", action_details(), seq=2)
        assert v.validate(e) == []

    def test_valid_scheduled(self):
        v = EvidenceValidator()
        e = make_event("scheduled", {"not_before": "2030-01-01T00:00:00+00:00"})
        assert v.validate(e) == []

    def test_valid_run_cancelled(self):
        v = EvidenceValidator()
        e = make_event("run_cancelled", {"reason": "cancellation_token"})
        assert v.validate(e) == []

    def test_valid_run_timeout(self):
        v = EvidenceValidator()
        e = make_event("run_timeout", {
            "reason": "deadline_exceeded",
            "deadline": "2026-01-01T00:00:00+00:00",
        })
        assert v.validate(e) == []


class TestValidatorMissingFields:
    def test_missing_field_in_action_event_detected(self):
        v = EvidenceValidator()
        details = action_details()
        del details["message"]
        e = make_event("action_succeeded", details)
        issues = v.validate(e)
        assert len(issues) == 1
        assert "message" in issues[0].missing
        assert issues[0].event_name == "action_succeeded"
        assert issues[0].unknown_event_type is False

    def test_missing_multiple_fields(self):
        v = EvidenceValidator()
        details = {"action": "act"}  # missing most fields
        e = make_event("action_failed", details)
        issues = v.validate(e)
        missing = issues[0].missing
        assert "principal_ref" in missing
        assert "request_id" in missing
        assert "message" in missing
        assert "idempotency_key" in missing
        assert "attempt" in missing
        assert "elapsed_seconds" in missing

    def test_missing_findings_in_verification_event(self):
        v = EvidenceValidator()
        details = verification_details()
        del details["findings"]
        e = make_event("precondition_verified", details)
        issues = v.validate(e)
        assert "findings" in issues[0].missing

    def test_missing_deadline_in_run_timeout(self):
        v = EvidenceValidator()
        e = make_event("run_timeout", {"reason": "deadline_exceeded"})  # no deadline
        issues = v.validate(e)
        assert "deadline" in issues[0].missing

    def test_empty_details_all_fields_missing(self):
        v = EvidenceValidator()
        e = make_event("action_succeeded", {})
        issues = v.validate(e)
        assert len(issues) == 1
        assert len(issues[0].missing) == 7  # all 7 required fields

    def test_none_details_all_fields_missing(self):
        v = EvidenceValidator()
        e = AuditEvent("action_succeeded", "step-1", "2026-01-01T00:00:00+00:00",
                       None, sequence_number=1)
        issues = v.validate(e)
        assert len(issues) == 1
        assert len(issues[0].missing) == 7


class TestUnknownEventTypes:
    def test_unknown_event_type_reported_by_default(self):
        v = EvidenceValidator()
        e = make_event("custom_domain_event", {"whatever": "value"})
        issues = v.validate(e)
        assert len(issues) == 1
        assert issues[0].unknown_event_type is True
        assert issues[0].missing == frozenset()

    def test_unknown_event_type_allowed_when_configured(self):
        v = EvidenceValidator(allow_unknown=True)
        e = make_event("custom_domain_event", {"whatever": "value"})
        assert v.validate(e) == []


class TestCustomSchema:
    def test_custom_schema_used(self):
        custom = {"my_event": frozenset({"x", "y"})}
        v = EvidenceValidator(schema=custom)
        # Valid
        e1 = make_event("my_event", {"x": 1, "y": 2})
        assert v.validate(e1) == []
        # Missing y
        e2 = make_event("my_event", {"x": 1})
        issues = v.validate(e2)
        assert "y" in issues[0].missing
        # The default schema's events are now unknown to this validator
        e3 = make_event("action_succeeded", action_details())
        issues3 = v.validate(e3)
        assert len(issues3) == 1
        assert issues3[0].unknown_event_type is True

    def test_custom_schema_with_allow_unknown(self):
        custom = {"my_event": frozenset({"x"})}
        v = EvidenceValidator(schema=custom, allow_unknown=True)
        # Unknown event type is allowed
        e = make_event("action_succeeded", action_details())
        assert v.validate(e) == []


class TestValidateChain:
    def test_clean_chain_no_issues(self):
        v = EvidenceValidator()
        events = [
            make_event("precondition_verified", verification_details(), seq=1),
            make_event("action_succeeded", action_details(), seq=2),
            make_event("postcondition_verified", verification_details(), seq=3),
        ]
        assert v.validate_chain(events) == []
        assert v.is_chain_valid(events) is True

    def test_chain_with_one_bad_event_reports_issue(self):
        v = EvidenceValidator()
        good_action = action_details()
        bad_action = action_details()
        del bad_action["request_id"]
        events = [
            make_event("precondition_verified", verification_details(), seq=1),
            make_event("action_succeeded", bad_action, seq=2),
            make_event("postcondition_verified", verification_details(), seq=3),
        ]
        issues = v.validate_chain(events)
        assert len(issues) == 1
        assert issues[0].sequence_number == 2
        assert "request_id" in issues[0].missing

    def test_chain_with_multiple_bad_events_reports_all(self):
        v = EvidenceValidator()
        events = [
            make_event("precondition_verified", {}, seq=1),  # missing all
            make_event("action_succeeded", action_details(), seq=2),  # ok
            make_event("postcondition_verified", {}, seq=3),  # missing all
        ]
        issues = v.validate_chain(events)
        assert len(issues) == 2
        assert issues[0].sequence_number == 1
        assert issues[1].sequence_number == 3

    def test_empty_chain_no_issues(self):
        v = EvidenceValidator()
        assert v.validate_chain([]) == []
        assert v.is_chain_valid([]) is True


class TestMalformedEventObjects:
    def test_event_without_event_attr_does_not_crash(self):
        v = EvidenceValidator()
        # A non-AuditEvent object — should not crash.
        issues = v.validate(object())  # type: ignore
        assert len(issues) == 1
        assert issues[0].unknown_event_type is True

    def test_event_with_non_mapping_details(self):
        v = EvidenceValidator()
        e = AuditEvent("action_succeeded", "step", "2026-01-01T00:00:00+00:00",
                       "not-a-mapping", sequence_number=1)
        issues = v.validate(e)
        assert len(issues) == 1
        assert len(issues[0].missing) == 7  # all fields missing


class TestEndToEndWithOrchestrator:
    def test_clean_run_produces_valid_evidence(self):
        """A successful orchestrator run produces audit events that
        pass evidence validation against the default schema."""
        dispatcher = ActionDispatcher({"act"})
        dispatcher.register("act", lambda r, c: ActionResult(
            ActionStatus.SUCCEEDED, "done",
            replace(c, observations={**c.observations,
                                     "done": Observation(True, source="result")})))
        step = WorkflowStep("s", "act", policy("pre"),
                           policy("post", Constraint("done", "eq", True)))
        result = ClosedLoopOrchestrator([step], dispatcher).run(state(done=False))
        v = EvidenceValidator()
        assert v.is_chain_valid(result.audit) is True

    def test_tampered_evidence_detected(self):
        """Removing a field from an action event's details is detected
        by the validator."""
        dispatcher = ActionDispatcher({"act"})
        dispatcher.register("act", lambda r, c: ActionResult(
            ActionStatus.SUCCEEDED, "done",
            replace(c, observations={**c.observations,
                                     "done": Observation(True, source="result")})))
        step = WorkflowStep("s", "act", policy("pre"),
                           policy("post", Constraint("done", "eq", True)))
        result = ClosedLoopOrchestrator([step], dispatcher).run(state(done=False))
        # Tamper: remove 'message' from the action event.
        tampered = list(result.audit)
        action_idx = next(i for i, e in enumerate(tampered) if e.event == "action_succeeded")
        tampered[action_idx] = replace(tampered[action_idx],
                                       details={k: v for k, v in tampered[action_idx].details.items()
                                                if k != "message"})
        v = EvidenceValidator()
        issues = v.validate_chain(tampered)
        assert len(issues) == 1
        assert "message" in issues[0].missing

    def test_audit_sink_events_also_pass_validation(self):
        """When an audit sink is configured, the chained events it
        records also pass evidence validation (the sink records the
        same details the orchestrator emits)."""
        sink = InMemoryAuditSink()
        dispatcher = ActionDispatcher({"act"})
        dispatcher.register("act", lambda r, c: ActionResult(
            ActionStatus.SUCCEEDED, "done",
            replace(c, observations={**c.observations,
                                     "done": Observation(True, source="result")})))
        step = WorkflowStep("s", "act", policy("pre"),
                           policy("post", Constraint("done", "eq", True)))
        ClosedLoopOrchestrator([step], dispatcher, audit_sink=sink).run(state(done=False))
        v = EvidenceValidator()
        assert v.is_chain_valid(sink.events) is True


class TestEvidenceIssueRepr:
    def test_issue_str_for_missing_fields(self):
        issue = EvidenceIssue(
            event_name="action_succeeded",
            sequence_number=3,
            missing=frozenset({"message", "request_id"}),
        )
        s = str(issue)
        assert "action_succeeded" in s
        assert "seq=3" in s
        assert "missing" in s

    def test_issue_str_for_unknown_event_type(self):
        issue = EvidenceIssue(
            event_name="custom_event",
            sequence_number=5,
            unknown_event_type=True,
        )
        s = str(issue)
        assert "unknown event type" in s
        assert "custom_event" in s
