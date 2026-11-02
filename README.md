# UPMP — Universal Process Monitoring Package

**Version 2.4.0** | MIT License

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

Use `StateSchema` and `SchemaRegistry` to validate versioned domain snapshots (field types, required fields, units, enumerations and numeric bounds). Verification policies also support inclusive `between` ranges and explicit `approx` tolerances. Unit conversion remains the adapter's responsibility.

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

## Find the step that needs attention

The workflow result contains the step-by-step audit trail. Use the standalone
diagnostic helper to turn that evidence into a concise report:

    from UPMP import diagnose_workflow

    diagnosis = diagnose_workflow(result)
    print(diagnosis.format_report())
    report_data = diagnosis.to_dict()

The report identifies observed steps, their outcome and attempts, the failed
precondition/action/postcondition or unmet dependency, relevant evidence, and
a suggested next check. It works with sequential and parallel results. It
does not dispatch repairs or claim a root cause that the evidence cannot show.

Run a self-contained example with:

    python examples/diagnose_workflow.py

## External security boundary

Credential storage, encryption, compression, logins and identity management belong to the separate security service. UPMP accepts an optional authorization callback at action dispatch and an opaque `principal_ref`; it does not accept or store passwords, API keys, encryption keys or tokens. The callback is the seam for the external authorization engine.

## Audit trail

The runtime produces an `AuditEvent` per workflow step. By default these
events are returned in `WorkflowResult.audit` and discarded when the
process exits. For durable, hash-chained audit storage, pass an
`AuditSink` to the orchestrator:

```python
from UPMP.audit import FileAuditSink, SQLiteAuditSink

# JSONL file (one event per line, fsync per append)
with FileAuditSink("audit.jsonl") as sink:
    ClosedLoopOrchestrator(steps, dispatcher, audit_sink=sink).run(state)
    sink.write_checkpoint("audit.checkpoint.json")  # trusted tip anchor

# Or single-file SQLite (PRIMARY KEY enforces monotonic seq at DB layer)
with SQLiteAuditSink("audit.db") as sink:
    ClosedLoopOrchestrator(steps, dispatcher, audit_sink=sink).run(state)
```

The hash chain **detects** tampering with any non-terminal event;
`write_checkpoint` / `verify_checkpoint` extend detection to the
terminal event and to truncation, **provided the checkpoint file is
stored in an independent trust domain** (out-of-band copy, signed
blob, or another host). `fsync` and SQLite ACID give crash durability,
**not** tamper-proof or append-only storage — those require OS-level
controls (`chattr +a`), WORM storage, or an external transparency log.
Full threat model and remaining limitations are documented in the
`UPMP/audit.py` module docstring.

## Current limits

- The numeric `MonitoringEngine` scores are heuristics. The typed `VerificationPolicy` is the hard pass/fail/indeterminate path.
- Handlers run in-process by default. Run cancellation and deadlines are cooperative in the core, and per-step timeout detection is post-hoc. Optional thread and process preemptible dispatchers have different limits: threads cannot be killed, while process handlers must be picklable top-level functions. Neither can undo external side effects already started.
- Retries require a step declared idempotent. An optional idempotency store can deduplicate action/key dispatches; without a configured store, the handler must handle external deduplication.
- Durable, hash-chained audit storage is provided (`FileAuditSink`, `SQLiteAuditSink`) with a checkpoint mechanism for terminal-event authentication.
- Audit storage is crash-durable but **not** tamper-proof against an attacker with write access to the storage file or database. Tamper **detection** is provided by the hash chain and the checkpoint mechanism; tamper **prevention** requires external controls (OS-level append-only flags, WORM storage, or a transparency log with an external verifier).
- Physical deployments still require independent local interlocks and domain-specific safe-failure behavior.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the contract, adapter boundary and implementation roadmap. Retired prototype implementations are not part of the supported UPMP source distribution.

