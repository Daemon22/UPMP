# UPMP — Universal Process Monitoring Package

**Version 2.4.0** | MIT License

UPMP provides domain-neutral building blocks for process integrity: state observation, verification against explicit criteria, risk and recovery handling, and workflow orchestration. It supports adapters for software, services, agents and equipment while keeping domain-specific sensors, policies and actions outside the core.

UPMP declares no third-party runtime dependencies. `pytest` and its support packages are needed only to run the test suite; optional signature tests also use `cryptography`.

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
a suggested next check. It works with sequential and parallel results, whether
actions use local handlers or optional adapters. It does not dispatch repairs
or claim a root cause that the evidence cannot show.

Run a self-contained example with:

    python examples/diagnose_workflow.py

## Reuse a process map and hand off a precise slice

UPMP can work alone as the process charter, live monitor, verifier, audit trail,
and point of escalation. An adapter supplies the process map and the connected
tools do the actual work. Every connection in the map must come from a declared
dependency/route or from recorded execution evidence; an adapter should leave
unknown paths unknown.

The graph API can represent workflow steps, application screens, repository
components, or other connected items. Attach stable source
references to nodes so a paired tool can resolve the exact part it owns:

```python
from UPMP.flow_graph import FlowCatalog, workflow_flow_graph

graph = workflow_flow_graph(
    steps,
    audit=result.audit,
    node_details_by_id={
        "payment": {
            "resource_refs": ["src/checkout/payment.py#PaymentStep"],
        },
    },
    entry_step_ids=("start",),
)
catalog = FlowCatalog()
catalog.register(
    "checkout",
    graph,
    metadata={"revision": "the source revision mapped by the adapter"},
)
catalog.save("process-map.json")

# A paired tool can load the saved map and request only this node's neighbors.
scope = FlowCatalog.load("process-map.json").select(
    "checkout",
    resource_ref="src/checkout/payment.py#PaymentStep",
    direction="both",
    max_depth=1,
)
handoff = scope.to_dict()
```

The selected slice includes its exact node IDs, nearby declared paths, and
boundary connections back to the rest of the process. A tool can use that
handoff to edit or inspect one component without rebuilding the whole map.
Afterward, UPMP can verify the tool's result against the step's postconditions
and record the route actually taken. A workflow can route failed or unverifiable
results to its configured user-contact step and wait for direction. The adapter
owns map freshness and source references; UPMP does not claim to discover
application behavior it has not observed or been given.

Applications have a dedicated screen and navigation adapter. An app connector
provides declared screens and routes, then passes observed navigation and the
current screen. UPMP highlights matched paths. If the app visits an undeclared
route or screen, the map shows an indeterminate route or screen so the missing
map entry can be reviewed:

```python
from UPMP import (
    ApplicationNavigation,
    ApplicationScreen,
    ApplicationTransition,
    application_flow_graph,
)

app_graph = application_flow_graph(
    screens=[
        ApplicationScreen("home", "Home", "/", resource_refs=["src/home.py#Home"]),
        ApplicationScreen("search", "Search", "/search"),
        ApplicationScreen("account", "Account", "/account"),
        ApplicationScreen("summary", "Summary", "/summary"),
    ],
    transitions=[
        ApplicationTransition("home", "search", "Search link", "home-search"),
        ApplicationTransition("home", "account", "Account link", "home-account"),
        ApplicationTransition("search", "summary", "Open summary", "search-summary"),
        ApplicationTransition("account", "summary", "Return to summary", "account-summary"),
    ],
    observed_navigation=browser_adapter.navigation_events,
    current_screen_id=browser_adapter.current_screen_id,
    entry_screen_ids=("home",),
    exit_screen_ids=("summary",),
)
```

Here the declared screen map shows both routes and their shared destination;
the connector's navigation events identify which path the app took. The
`browser_adapter` name represents an app-specific connector that supplies
screen IDs and navigation evidence; UPMP does not discover arbitrary app
routes without one.

## Monitor Craft Engine operations

Craft Engine's `nano` and `macro` operations can append a content-free JSONL
progress stream. UPMP reads that stream into the actual pack or restore flow,
including the metadata-encryption and self-verification branches, and marks
the route recorded by the current run:

```python
from UPMP import craft_progress_flow_graph, read_craft_progress_jsonl

events = read_craft_progress_jsonl("craft-progress.jsonl")
graph = craft_progress_flow_graph(events)  # pass operation_id if the file has several runs
catalog.register("craft-pack", graph)
```

Start the CRAFT CLI with `--events craft-progress.jsonl` to create the stream.
The two integrations stay separate: CRAFT owns encryption and artifact
creation, UPMP checks and maps its progress, Vault can own artifact storage
later, and Keeper of Secrets can provide credentials later. The progress file
contains stage evidence only; it never contains the artifact, file contents,
filename, passphrase, or credential key.

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

