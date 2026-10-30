"""M1.5 tests — orchestrator audit integration.

Proves that audit evidence is recorded during the real process
lifecycle: every event the orchestrator generates reaches the sink,
carries valid hash-chain metadata, and sink failures abort the run
loudly (fail-loud policy) instead of producing a result with a
silently truncated evidence trail.
"""
from dataclasses import replace

import pytest

from UPMP.audit import (
    FileAuditSink,
    InMemoryAuditSink,
    load_audit_file,
    verify_audit_file,
    verify_checkpoint,
    write_checkpoint,
)
from UPMP.closed_loop import (
    ActionDispatcher,
    ActionResult,
    ActionStatus,
    ClosedLoopOrchestrator,
    RunStatus,
    WorkflowStep,
)
from UPMP.contracts import (
    Constraint,
    FindingStatus,
    Observation,
    ProcessState,
    VerificationPolicy,
)


def state(**values):
    observations = {
        key: value if isinstance(value, Observation) else Observation(value, source="test")
        for key, value in values.items()
    }
    observations.setdefault("_test_guard", Observation(True, source="fixture"))
    return ProcessState("test.domain", "1", observations)


def policy(name, *constraints):
    if not constraints:
        constraints = (Constraint("_test_guard", "eq", True),)
    return VerificationPolicy(name, "1", constraints)


def make_dispatcher():
    dispatcher = ActionDispatcher({"act"})
    dispatcher.register("act", lambda req, current: ActionResult(
        ActionStatus.SUCCEEDED, "done",
        replace(current, observations={
            **current.observations, "done": Observation(True, source="action-result")})
    ))
    return dispatcher


SUCCESS_STEP = WorkflowStep("s", "act", policy("pre"),
                            policy("post", Constraint("done", "eq", True)))


class ExplodingSink:
    """Delegates to InMemoryAuditSink but refuses the 3rd append —
    simulates a storage failure mid-run."""

    def __init__(self):
        self._mem = InMemoryAuditSink()

    def append(self, event):
        if len(self._mem) >= 2:
            raise RuntimeError("simulated audit storage failure")
        return self._mem.append(event)

    def verify_chain(self, *args, **kwargs):
        return self._mem.verify_chain(*args, **kwargs)

    @property
    def events(self):
        return self._mem.events


class TestOrchestratorAuditIntegration:
    def test_run_records_every_event_to_sink_with_valid_chain(self):
        sink = InMemoryAuditSink()
        result = ClosedLoopOrchestrator([SUCCESS_STEP], make_dispatcher(),
                                        audit_sink=sink).run(state(done=False))
        assert result.status == RunStatus.COMPLETED
        names = [e.event for e in sink.events]
        assert names == ["precondition_verified", "action_succeeded",
                         "postcondition_verified"]
        assert [e.sequence_number for e in sink.events] == [1, 2, 3]
        assert sink.verify_chain() is True

    def test_sink_events_match_result_audit_payloads(self):
        sink = InMemoryAuditSink()
        result = ClosedLoopOrchestrator([SUCCESS_STEP], make_dispatcher(),
                                        audit_sink=sink).run(state(done=False))
        assert len(sink.events) == len(result.audit)
        for chained, plain in zip(sink.events, result.audit):
            assert chained.event == plain.event
            assert chained.step_id == plain.step_id
            assert chained.timestamp == plain.timestamp
            assert dict(chained.details) == dict(plain.details)

    def test_no_sink_keeps_legacy_behavior(self):
        result = ClosedLoopOrchestrator([SUCCESS_STEP], make_dispatcher()).run(state(done=False))
        assert result.status == RunStatus.COMPLETED
        assert len(result.audit) == 3
        for event in result.audit:  # unchained, exactly as before M1.5
            assert event.sequence_number == 0
            assert event.prev_hash == ""

    def test_blocked_run_records_precondition_and_block_reason(self):
        sink = InMemoryAuditSink()
        step = WorkflowStep("s", "act", policy("guard", Constraint("safe", "eq", True)),
                            policy("post"))
        result = ClosedLoopOrchestrator([step], make_dispatcher(), audit_sink=sink).run(state(safe=False))
        assert result.status == RunStatus.FAILED
        names = [e.event for e in sink.events]
        assert names == ["precondition_verified", "dispatch_blocked"]
        assert sink.events[-1].details == {"reason": "fail"}
        assert sink.verify_chain() is True

    def test_retry_attempts_all_recorded(self):
        sink = InMemoryAuditSink()
        dispatcher = ActionDispatcher({"retryable"})
        seen = []

        def flaky(request, current):
            seen.append(request.idempotency_key)
            if len(seen) == 1:
                return ActionResult(ActionStatus.FAILED, "temporary error")
            return ActionResult(ActionStatus.SUCCEEDED, "done", current)

        dispatcher.register("retryable", flaky)
        step = WorkflowStep("retry", "retryable", policy("pre"), policy("post"),
                            max_retries=1, idempotent=True)
        result = ClosedLoopOrchestrator([step], dispatcher, audit_sink=sink).run(state())
        assert result.status == RunStatus.COMPLETED
        names = [e.event for e in sink.events]
        assert names == ["precondition_verified", "action_failed",
                         "action_succeeded", "postcondition_verified"]
        assert sink.events[1].details["attempt"] == 1
        assert sink.events[2].details["attempt"] == 2
        assert sink.verify_chain() is True

    def test_scheduled_run_records_scheduled_event(self):
        sink = InMemoryAuditSink()
        from datetime import datetime, timezone
        step = WorkflowStep("later", "act", policy("pre"), policy("post"),
                            not_before="2030-01-01T00:00:00+00:00")
        future = datetime(2029, 1, 1, tzinfo=timezone.utc)
        result = ClosedLoopOrchestrator([step], make_dispatcher(), audit_sink=sink).run(state(), future)
        assert result.status == RunStatus.SCHEDULED
        assert [e.event for e in sink.events] == ["scheduled"]
        assert sink.verify_chain() is True


class TestFailLoudPolicy:
    def test_sink_failure_aborts_run_without_result(self):
        sink = ExplodingSink()
        with pytest.raises(RuntimeError, match="simulated audit storage failure"):
            ClosedLoopOrchestrator([SUCCESS_STEP], make_dispatcher(),
                                   audit_sink=sink).run(state(done=False))
        # Events accepted before the failure remain recorded and valid.
        assert len(sink.events) == 2
        assert sink.verify_chain() is True

    def test_invalid_sink_rejected_at_construction(self):
        with pytest.raises(TypeError, match="append"):
            ClosedLoopOrchestrator([SUCCESS_STEP], make_dispatcher(), audit_sink=42)

    def test_sink_object_without_append_rejected(self):
        with pytest.raises(TypeError):
            ClosedLoopOrchestrator([SUCCESS_STEP], make_dispatcher(), audit_sink=object())


class TestEndToEndLifecycle:
    def test_orchestrator_to_file_to_checkpoint(self, tmp_path):
        """Full M1.5 acceptance path: a real run's evidence lands in
        durable storage, survives close/reload, verifies forensically,
        and is checkpointed for last-event authentication."""
        chain = tmp_path / "audit.jsonl"
        checkpoint = tmp_path / "checkpoint.json"

        with FileAuditSink(chain) as sink:
            result = ClosedLoopOrchestrator([SUCCESS_STEP], make_dispatcher(),
                                            audit_sink=sink).run(state(done=False))
            assert result.status == RunStatus.COMPLETED
            sink.write_checkpoint(checkpoint)

        # Forensic verification of the file written during the run.
        assert verify_audit_file(chain) is True
        events = load_audit_file(chain)
        assert [e.event for e in events] == ["precondition_verified",
                                             "action_succeeded",
                                             "postcondition_verified"]
        assert [e.sequence_number for e in events] == [1, 2, 3]
        # Trusted checkpoint confirms the run's evidence, tip included.
        assert verify_checkpoint(events, checkpoint) is True

    def test_tampered_evidence_detected_after_the_fact(self, tmp_path):
        chain = tmp_path / "audit.jsonl"
        checkpoint = tmp_path / "checkpoint.json"
        with FileAuditSink(chain) as sink:
            ClosedLoopOrchestrator([SUCCESS_STEP], make_dispatcher(),
                                   audit_sink=sink).run(state(done=False))
            sink.write_checkpoint(checkpoint)

        events = load_audit_file(chain)
        # Internal chain check alone CANNOT see last-event tampering.
        from UPMP.audit import InMemoryAuditSink as _Mem
        mem = _Mem()
        mem.events = list(events[:-1]) + [replace(events[-1], details={"status": "pass", "policy": "post", "version": "1", "findings": []})]
        assert mem.verify_chain() is True  # blind spot of the bare chain
        # The independently stored checkpoint DOES see it.
        assert verify_checkpoint(mem.events, checkpoint) is False
