"""Domain-neutral closed-loop example using a simulated irrigation system.

This example has no hardware access. It shows orchestration requests and
verifies simulated state updates only.
"""

from dataclasses import replace
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from UPMP import (
    ActionDispatcher, ActionResult, ActionStatus, ClosedLoopOrchestrator,
    Constraint, Observation, ProcessState, VerificationPolicy, WorkflowStep,
)


def policy(name, *constraints):
    return VerificationPolicy(name, "1", constraints)


def main():
    state = ProcessState("irrigation", "1", {
        "mains_power": Observation(False, source="simulator"),
        "generator_available": Observation(True, source="simulator"),
        "generator_on": Observation(False, source="simulator"),
        "water_available": Observation(True, source="simulator"),
        "pump_running": Observation(False, source="simulator"),
    })

    def start_generator(request, before):
        values = dict(before.observations)
        values["generator_on"] = Observation(True, source="simulated-action")
        return ActionResult(ActionStatus.SUCCEEDED, "simulated generator started", replace(before, observations=values))

    def start_pump(request, before):
        values = dict(before.observations)
        values["pump_running"] = Observation(True, source="simulated-action")
        return ActionResult(ActionStatus.SUCCEEDED, "simulated pump started", replace(before, observations=values))

    dispatcher = ActionDispatcher({"start_generator", "start_pump"})
    dispatcher.register("start_generator", start_generator)
    dispatcher.register("start_pump", start_pump)
    generator = WorkflowStep(
        "backup_power", "start_generator",
        policy("generator-pre", Constraint("mains_power", "eq", False), Constraint("generator_available", "eq", True)),
        policy("generator-post", Constraint("generator_on", "eq", True)),
        on_success="irrigate",
    )
    pump = WorkflowStep(
        "irrigate", "start_pump",
        policy("pump-pre", Constraint("generator_on", "eq", True), Constraint("water_available", "eq", True)),
        policy("pump-post", Constraint("pump_running", "eq", True)),
    )
    result = ClosedLoopOrchestrator([generator, pump], dispatcher).run(state)
    print("Simulation result:", result.status.value)
    print("Dispatches:", [action.message for action in result.actions])
    print("Final verification:", result.verification.status.value if result.verification else "none")
    print("Audit events:", [event.event for event in result.audit])


if __name__ == "__main__":
    main()

