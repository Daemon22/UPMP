import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from UPMP import (
    ActionDispatcher, ActionResult, CheckpointStatus, ClosedLoopOrchestrator,
    Constraint, GoalOutcome, Observation, ProcessCharter,
    ProcessPart, ProcessRunTracker, ProcessState, VerificationPolicy,
    WorkflowStep, build_process_run_view,
)


def state(phase="ready"):
    return ProcessState("demo", "1", {
        "ready": Observation(True, source="test"),
        "phase": Observation(phase, source="test"),
    })


def policy(policy_id, field, value):
    return VerificationPolicy(policy_id, "1", (Constraint(field, "eq", value),))


class ObservatoryTests(unittest.TestCase):
    def make_charter(self, expected="done", parts=()):
        return ProcessCharter(
            "demo.process", "1", "Finish the declared demo process",
            policy("goal", "phase", expected), tuple(parts),
        )

    def make_step(self, post="done"):
        return WorkflowStep(
            "finish", "finish", policy("ready", "ready", True),
            policy("finished", "phase", post), description="Finish process",
        )

    def make_dispatcher(self, value="done"):
        dispatcher = ActionDispatcher(["finish"])
        dispatcher.register("finish", lambda request, current: ActionResult(
            "succeeded", "finished", state(
                "done" if value == "done" else value,
            ),
        ))
        return dispatcher

    def run_step(self, charter, step=None, value="done", metadata=True):
        step = step or self.make_step()
        audit_metadata = charter.audit_metadata if metadata else None
        result = ClosedLoopOrchestrator(
            [step], self.make_dispatcher(value), audit_metadata=audit_metadata,
        ).run(state())
        return result

    def test_charter_is_immutable_and_hash_tracks_declared_goal(self):
        expected = {"route": ["validate", "finish"]}
        charter = ProcessCharter(
            "demo.process", "1", "Finish",
            VerificationPolicy("goal", "1", (Constraint("phase", "eq", expected),)),
        )
        before = charter.charter_hash
        expected["route"].append("invented")
        self.assertEqual(charter.charter_hash, before)
        with self.assertRaises(TypeError):
            charter.acceptance_policy.constraints[0].expected["new"] = True

    def test_runtime_metadata_binds_events_and_goal_requires_binding(self):
        charter = self.make_charter()
        step = self.make_step()
        bound = self.run_step(charter, step)
        view = build_process_run_view(charter, [step], bound)
        self.assertTrue(view.charter_bound)
        self.assertEqual(view.goal_outcome, GoalOutcome.VERIFIED)
        self.assertEqual(view.progress_percent, 100.0)
        self.assertTrue(all(e.details["run_id"] == view.run_id for e in bound.audit))

        unbound = self.run_step(charter, step, metadata=False)
        unbound_view = build_process_run_view(charter, [step], unbound)
        self.assertFalse(unbound_view.charter_bound)
        self.assertEqual(unbound_view.goal_outcome, GoalOutcome.INDETERMINATE)

    def test_failed_checkpoint_is_visible_and_never_counts_as_success(self):
        charter = self.make_charter()
        step = self.make_step(post="never")
        result = self.run_step(charter, step)
        view = build_process_run_view(charter, [step], result)
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(view.steps[0].checkpoints[0].status, CheckpointStatus.PASSED)
        self.assertEqual(view.steps[0].checkpoints[1].status, CheckpointStatus.FAILED)
        self.assertEqual(view.progress_percent, 50.0)
        self.assertEqual(view.goal_outcome, GoalOutcome.INDETERMINATE)
        self.assertEqual(len(view.deviations), 1)

    def test_nested_parts_roll_up_checkpoint_progress_and_declared_weights(self):
        parts = (
            ProcessPart("root", "Whole process"),
            ProcessPart("identity", "Identity stream", "root", weight=2),
            ProcessPart("finish-part", "Finish", "identity", "finish"),
        )
        charter = self.make_charter(parts=parts)
        step = self.make_step()
        result = self.run_step(charter, step)
        view = build_process_run_view(charter, [step], result)
        root = next(p for p in view.parts if p.part_id == "root")
        identity = next(p for p in view.parts if p.part_id == "identity")
        self.assertEqual(root.progress_percent, 100.0)
        self.assertEqual(root.progress_method, "declared_weights")
        self.assertEqual(identity.status, "succeeded")

    def test_parts_must_map_every_workflow_step_once(self):
        charter = self.make_charter(parts=(
            ProcessPart("wrong", "Wrong workflow step", step_id="wrong-step"),
        ))
        with self.assertRaises(ValueError):
            build_process_run_view(charter, [self.make_step()], self.run_step(charter))

    def test_tracker_publishes_intermediate_checkpoint_progress(self):
        charter = self.make_charter()
        step = self.make_step()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "run.json"
            tracker = ProcessRunTracker(charter, [step], state(), snapshot)
            with patch("UPMP.observatory.write_process_run_snapshot") as write_snapshot:
                tracker.begin()
                dispatcher = ActionDispatcher(["finish"])
                observed = []

                def finish(request, current):
                    observed.append(tracker.current_view)
                    return ActionResult("succeeded", "finished", state("done"))

                dispatcher.register("finish", finish)
                result = ClosedLoopOrchestrator(
                    [step], dispatcher, audit_sink=tracker,
                    audit_metadata=charter.audit_metadata,
                ).run(state())
                self.assertEqual(observed[0].run_status, "running")
                self.assertEqual(observed[0].progress_percent, 50.0)
                self.assertEqual(observed[0].goal_outcome, GoalOutcome.INDETERMINATE)
                self.assertEqual(result.status.value, "completed")
                tracker.record_mutation(
                    "mutation-1", "finish", {"field": "phase", "reason": "external observation"},
                )
                final = tracker.finish(result)
            self.assertGreaterEqual(write_snapshot.call_count, 4)
            self.assertEqual(final.goal_verification["status"], "pass", final.goal_verification)
            self.assertEqual(final.goal_outcome, GoalOutcome.VERIFIED)
            self.assertEqual(final.mutations[0]["mutation_id"], "mutation-1")
            self.assertEqual(final.to_dict()["run"]["run_status"], "completed")


if __name__ == "__main__":
    unittest.main()
