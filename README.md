# UPMP — Universal Process Monitoring Package

**Version 2.3.0** | MIT License

UPMP provides domain-neutral building blocks for process integrity: state observation, verification against explicit criteria, risk and recovery handling, and workflow orchestration. It supports adapters for software, services, agents and equipment while keeping domain-specific sensors, policies and actions outside the core.

## The four pillars

| Pillar | Responsibility |
|---|---|
| **Determine** | Represent what is observed now, with time, source, unit and quality. |
| **Verify** | Check observations against intent, desired state, hard constraints and allowed transitions. |
| **Insure** | Assess drift and risk; recommend or route containment and recovery. |
| **Orchestrate** | Dispatch authorized work, verify the result, then begin the loop again. |

```text
Determine → Verify → Insure → Orchestrate → observe result → Determine
```

The framework can grow through adapters and hierarchical workflows. Domain values are normalized at adapter boundaries; the core does not assume that a pump, compiler, agent or service has the same state model.

## Quick start

Install the package from this directory:

```bash
python -m pip install .
```

Use the typed verification and workflow interfaces:

```python
from UPMP import (
    ActionDispatcher, ActionResult, ActionStatus, ClosedLoopOrchestrator,
    Constraint, Observation, ProcessState, VerificationPolicy, WorkflowStep,
)

state = ProcessState("demo", "1", {
    "ready": Observation(True, source="demo-sensor"),
    "complete": Observation(False, source="demo-sensor"),
})

def run_demo(request, current):
    values = dict(current.observations)
    values["complete"] = Observation(True, source="demo-action-result")
    return ActionResult(ActionStatus.SUCCEEDED, "done", ProcessState("demo", "1", values))

dispatcher = ActionDispatcher({"run_demo"})
dispatcher.register("run_demo", run_demo)
policy = lambda name, *checks: VerificationPolicy(name, "1", checks)
step = WorkflowStep(
    "work", "run_demo",
    policy("before", Constraint("ready", "eq", True)),
    policy("after", Constraint("complete", "eq", True)),
)
result = ClosedLoopOrchestrator([step], dispatcher).run(state)
print(result.status.value)  # completed only after postcondition verification
```

For a complete simulated fallback workflow, run:

```bash
python examples/closed_loop_simulation.py
```

The simulation switches to a simulated generator when mains power is unavailable, then verifies the pump state. It has no hardware access.

## External security boundary

Credential storage, encryption, compression, logins and identity management belong to the separate security service. UPMP accepts an optional authorization callback at action dispatch and an opaque `principal_ref`; it does not accept or store passwords, API keys, encryption keys or tokens. The callback is the seam for the external authorization engine.

## Current limits

- The numeric `MonitoringEngine` scores are heuristics. The typed `VerificationPolicy` is the hard pass/fail/indeterminate path.
- The workflow runtime is in-process and keeps its audit events in memory. Action handlers run synchronously; persistent audit, safe cancellation and timeout guarantees are not provided.
- A handler must enforce idempotency for retried actions. The runtime reuses the idempotency key but cannot enforce external deduplication.
- Physical deployments still require independent local interlocks and domain-specific safe-failure behavior.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the contract, adapter boundary and implementation roadmap. Retired prototype implementations are not part of the supported UPMP source distribution.

