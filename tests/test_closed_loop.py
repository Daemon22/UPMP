import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from UPMP import (
    ActionDispatcher, ActionResult, ActionStatus, ClosedLoopOrchestrator,
    Constraint, DataQuality, FieldSchema, FindingStatus, Observation, ProcessState,
    SchemaRegistry, StateSchema,
    RunStatus, VerificationPolicy, WorkflowStep,
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


class VerificationTests(unittest.TestCase):
    def test_hard_constraint_pass_fail_and_missing_indeterminate(self):
        p = policy("pressure", Constraint("pressure", "lte", 10, unit="kPa"))
        self.assertEqual(p.evaluate(state(pressure=Observation(8, source="sensor", unit="kPa"))).status, FindingStatus.PASS)
        self.assertEqual(p.evaluate(state(pressure=Observation(12, source="sensor", unit="kPa"))).status, FindingStatus.FAIL)
        self.assertEqual(p.evaluate(state()).status, FindingStatus.INDETERMINATE)

    def test_stale_invalid_and_wrong_unit_never_pass(self):
        now = datetime.now(timezone.utc)
        p = policy("fresh", Constraint("pressure", "lte", 10, unit="kPa", max_age_seconds=1))
        stale = Observation(2, (now - timedelta(seconds=10)).isoformat(), "sensor", "kPa")
        invalid = Observation(2, source="sensor", unit="kPa", quality=DataQuality.INVALID)
        wrong_unit = Observation(2, source="sensor", unit="psi")
        for value in (stale, invalid, wrong_unit):
            self.assertEqual(p.evaluate(state(pressure=value), now).status, FindingStatus.INDETERMINATE)

    def test_required_observation_missing_fails(self):
        self.assertEqual(policy("required", Constraint("power", "exists")).evaluate(state()).status, FindingStatus.FAIL)

    def test_empty_policy_cannot_authorize_action(self):
        self.assertEqual(VerificationPolicy("empty", "1").evaluate(state()).status, FindingStatus.INDETERMINATE)

    def test_range_and_tolerance_constraints(self):
        p = policy("targets", Constraint("temperature", "between", (18, 24), unit="C"),
                   Constraint("target", "approx", 20, tolerance=0.25, unit="C"))
        self.assertEqual(p.evaluate(state(temperature=Observation(24, source="sensor", unit="C"),
                                       target=Observation(20.2, source="sensor", unit="C"))).status,
                         FindingStatus.PASS)
        self.assertEqual(p.evaluate(state(temperature=Observation(24.1, source="sensor", unit="C"),
                                       target=Observation(20.2, source="sensor", unit="C"))).status,
                         FindingStatus.FAIL)
        self.assertEqual(p.evaluate(state(temperature=Observation(20, source="sensor", unit="C"),
                                       target=Observation(20.26, source="sensor", unit="C"))).status,
                         FindingStatus.FAIL)

    def test_range_and_approximation_configuration_is_explicit(self):
        with self.assertRaises(ValueError):
            Constraint("x", "between", (1,))
        with self.assertRaises(ValueError):
            Constraint("x", "approx", 1)

    def test_versioned_schema_validation(self):
        schema = StateSchema("irrigation", "2", {
            "mains": FieldSchema("boolean"),
            "pressure": FieldSchema("number", unit="kPa", minimum=0, maximum=500),
            "mode": FieldSchema("string", enum=("idle", "watering", "fault")),
            "optional": FieldSchema("integer", required=False),
        })
        registry = SchemaRegistry([schema])
        valid = ProcessState("irrigation", "2", {
            "mains": Observation(False, source="relay"),
            "pressure": Observation(150, source="gauge", unit="kPa"),
            "mode": Observation("idle", source="controller"),
        })
        self.assertTrue(registry.validate(valid).valid)
        invalid = ProcessState("irrigation", "2", {
            "mains": Observation(0, source="relay"),
            "pressure": Observation(501, source="gauge", unit="psi"),
            "mode": Observation("unknown", source="controller"),
            "extra": Observation(True, source="controller"),
        })
        result = registry.validate(invalid)
        self.assertFalse(result.valid)
        self.assertTrue(any("mains must have type boolean" in error for error in result.errors))
        self.assertTrue(any("pressure must use unit kPa" in error for error in result.errors))
        self.assertTrue(any("pressure is above maximum" in error for error in result.errors))
        self.assertTrue(any("mode must be one" in error for error in result.errors))
        self.assertTrue(any("undeclared observation" in error for error in result.errors))
        self.assertFalse(registry.validate(ProcessState("irrigation", "3", {})).valid)

    def test_registry_rejects_duplicate_versions(self):
        schema = StateSchema("x", "1", {})
        with self.assertRaises(ValueError):
            SchemaRegistry([schema, schema])


class OrchestrationTests(unittest.TestCase):
    def test_closed_loop_success_and_observe_each_action(self):
        dispatcher = ActionDispatcher({"generator", "pump"})
        calls = []

        def generator(req, current):
            calls.append(req.action)
            values = dict(current.observations)
            values["generator_on"] = Observation(True, source="action-result")
            return ActionResult(ActionStatus.SUCCEEDED, "generator ready", replace(current, observations=values))

        def pump(req, current):
            calls.append(req.action)
            values = dict(current.observations)
            values["pump_on"] = Observation(True, source="action-result")
            return ActionResult(ActionStatus.SUCCEEDED, "pump running", replace(current, observations=values))

        dispatcher.register("generator", generator)
        dispatcher.register("pump", pump)
        steps = [
            WorkflowStep("backup", "generator", policy("backup-guard", Constraint("mains", "eq", False), Constraint("generator_available", "eq", True)), policy("backup-check", Constraint("generator_on", "eq", True)), on_success="irrigate"),
            WorkflowStep("irrigate", "pump", policy("pump-guard", Constraint("generator_on", "eq", True), Constraint("water", "eq", True)), policy("pump-check", Constraint("pump_on", "eq", True))),
        ]
        result = ClosedLoopOrchestrator(steps, dispatcher).run(state(mains=False, generator_available=True, generator_on=False, water=True, pump_on=False))
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(calls, ["generator", "pump"])
        self.assertEqual(sum(event.event == "postcondition_verified" for event in result.audit), 2)

    def test_failed_precondition_blocks_action(self):
        dispatcher = ActionDispatcher({"act"})
        called = []
        dispatcher.register("act", lambda req, current: called.append(req))
        step = WorkflowStep("s", "act", policy("guard", Constraint("safe", "eq", True)), policy("post"))
        result = ClosedLoopOrchestrator([step], dispatcher).run(state(safe=False))
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(called, [])

    def test_indeterminate_and_unauthorized_block_action(self):
        dispatcher = ActionDispatcher({"allowed"})
        called = []
        dispatcher.register("allowed", lambda req, current: called.append(req))
        step = WorkflowStep("s", "allowed", policy("missing", Constraint("sensor", "eq", True)), policy("post"))
        self.assertEqual(ClosedLoopOrchestrator([step], dispatcher).run(state()).status, RunStatus.BLOCKED)
        unauthorized = WorkflowStep("other", "not-allowed", policy("pre"), policy("post"))
        self.assertEqual(ClosedLoopOrchestrator([unauthorized], dispatcher).run(state()).status, RunStatus.BLOCKED)
        self.assertEqual(called, [])

    def test_external_authorization_hook_fails_closed(self):
        calls = []
        seen_principals = []
        dispatcher = ActionDispatcher({"act"}, authorize=lambda request, current: seen_principals.append(request.principal_ref) or request.principal_ref == "operator-123")

        def handle(request, current):
            calls.append(request)
            return ActionResult(ActionStatus.SUCCEEDED, "done", current)

        dispatcher.register("act", handle)
        step = WorkflowStep("s", "act", policy("pre"), policy("post"))
        result = ClosedLoopOrchestrator([step], dispatcher).run(state(), principal_ref="operator-123")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(seen_principals, ["operator-123"])

        result = ClosedLoopOrchestrator([step], dispatcher).run(state(), principal_ref="operator-456")
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.actions[0].status, ActionStatus.DENIED)
        self.assertEqual(len(calls), 1)

        broken = ActionDispatcher({"act"}, authorize=lambda request, current: (_ for _ in ()).throw(RuntimeError("offline")))
        broken.register("act", handle)
        result = ClosedLoopOrchestrator([step], broken).run(state())
        self.assertEqual(result.actions[0].status, ActionStatus.DENIED)
        self.assertEqual(len(calls), 1)

    def test_action_failure_can_enter_recovery_step(self):
        dispatcher = ActionDispatcher({"primary", "recover"})
        dispatcher.register("primary", lambda req, current: (_ for _ in ()).throw(RuntimeError("simulated fault")))
        dispatcher.register("recover", lambda req, current: ActionResult(ActionStatus.SUCCEEDED, "recovered", current))
        steps = [
            WorkflowStep("primary", "primary", policy("pre"), policy("post"), on_failure="recovery"),
            WorkflowStep("recovery", "recover", policy("recovery-pre"), policy("recovery-post")),
        ]
        result = ClosedLoopOrchestrator(steps, dispatcher).run(state())
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.actions[0].status, ActionStatus.FAILED)
        self.assertEqual(result.actions[1].status, ActionStatus.SUCCEEDED)

    def test_action_must_produce_verified_result(self):
        dispatcher = ActionDispatcher({"act"})
        dispatcher.register("act", lambda req, current: ActionResult(ActionStatus.SUCCEEDED, "accepted", current))
        step = WorkflowStep("s", "act", policy("pre"), policy("post", Constraint("done", "eq", True)))
        result = ClosedLoopOrchestrator([step], dispatcher).run(state(done=False))
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.verification.status, FindingStatus.FAIL)

    def test_schedule_and_cycle_limit(self):
        dispatcher = ActionDispatcher({"act"})
        called = []
        dispatcher.register("act", lambda req, current: (called.append(req), ActionResult(ActionStatus.SUCCEEDED, "ok", current))[1])
        scheduled = WorkflowStep("later", "act", policy("pre"), policy("post"), not_before="2030-01-01T00:00:00+00:00")
        future = datetime(2029, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(ClosedLoopOrchestrator([scheduled], dispatcher).run(state(), future).status, RunStatus.SCHEDULED)
        loop = WorkflowStep("loop", "act", policy("pre"), policy("post"), on_success="loop")
        result = ClosedLoopOrchestrator([loop], dispatcher, max_steps=2).run(state())
        self.assertEqual(result.status, RunStatus.STEP_LIMIT)
        self.assertEqual(len(called), 2)

    def test_retries_are_bounded_and_reuse_idempotency_key(self):
        dispatcher = ActionDispatcher({"retryable"})
        seen = []

        def flaky(request, current):
            seen.append(request.idempotency_key)
            if len(seen) == 1:
                return ActionResult(ActionStatus.FAILED, "temporary error")
            return ActionResult(ActionStatus.SUCCEEDED, "done", current)

        dispatcher.register("retryable", flaky)
        step = WorkflowStep("retry", "retryable", policy("pre"), policy("post"), max_retries=1, idempotent=True)
        result = ClosedLoopOrchestrator([step], dispatcher).run(state())
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(len(result.actions), 2)

    def test_non_idempotent_action_cannot_be_retried(self):
        with self.assertRaises(ValueError):
            WorkflowStep("retry", "unsafe", policy("pre"), policy("post"), max_retries=1)

    def test_bad_transition_rejected(self):
        step = WorkflowStep("s", "act", policy("pre"), policy("post"), on_success="missing")
        with self.assertRaises(ValueError):
            ClosedLoopOrchestrator([step], ActionDispatcher())


if __name__ == "__main__":
    unittest.main()

