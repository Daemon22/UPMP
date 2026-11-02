"""Run a local, simulated workflow and report the exact failed step.

This example uses no external platform or hardware. The simulated action
returns successfully, but its required output condition is false, so UPMP
reports that the workflow did not complete successfully.
"""
from dataclasses import replace

from UPMP import (
    ActionDispatcher,
    ActionResult,
    ActionStatus,
    ClosedLoopOrchestrator,
    Constraint,
    Observation,
    ProcessState,
    VerificationPolicy,
    WorkflowStep,
    diagnose_workflow,
)


def main() -> None:
    initial = ProcessState("release-build", "1", {
        "source_ready": Observation(True, source="simulation"),
        "artifact_valid": Observation(False, source="simulation"),
    })

    def build(request, current):
        observations = dict(current.observations)
        # Simulate a build action that ran, but produced an invalid artifact.
        observations["artifact_valid"] = Observation(False, source="simulated-check")
        return ActionResult(
            ActionStatus.SUCCEEDED,
            "build command returned; artifact validation failed",
            replace(current, observations=observations),
            output={"build_command_returned": True},
        )

    dispatcher = ActionDispatcher({"build"})
    dispatcher.register("build", build)
    step = WorkflowStep(
        "build_artifact",
        "build",
        VerificationPolicy("build-preconditions", "1", (
            Constraint("source_ready", "eq", True),
        )),
        VerificationPolicy("artifact-check", "1", (
            Constraint("artifact_valid", "eq", True),
        )),
        description="Build and verify a release artifact",
    )

    result = ClosedLoopOrchestrator([step], dispatcher).run(initial)
    diagnosis = diagnose_workflow(result)
    print(diagnosis.format_report())


if __name__ == "__main__":
    main()
