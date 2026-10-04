# UPMP Four-Pillar Architecture Contract

## Purpose

UPMP is a domain-independent process integrity framework. It defines a loop for observing a process, checking it against its charter, managing risk, and coordinating authorized work. It does not prescribe a domain's sensors, success rules, equipment, or execution runtime.

The four pillar names and responsibilities below are intended to remain stable as domain adapters and implementations grow. Complexity belongs in composable policies, adapters, and child workflows rather than in domain-specific branches inside the shared core.

## Pillar contract

| Pillar | Question | Required inputs | Required result |
|---|---|---|---|
| **Determine** | What is the process state now? | Domain observations, observation time, source and quality metadata, state schema. | A typed state snapshot and derived measures with provenance and uncertainty where available. |
| **Verify** | Is the observed process acceptable relative to its intent? | Intent/version, desired state, constraints, current and prior snapshots, verification policy. | Pass, fail, or indeterminate findings with evidence, severity and policy/version references. |
| **Insure** | What should happen to contain or recover from risk? | Verification findings, hazard limits, recovery policy, checkpoints and available capabilities. | A recommendation or approved recovery plan, or escalation/stop recommendation. It must not silently execute an action. |
| **Orchestrate** | Which authorized step runs next, and how is its result handled? | Workflow definition, schedule/event, verified state, authorization, available action adapters. | A recorded dispatch decision, observed action result, and explicit transition or terminal state. |

The control loop is:

```text
Determine → Verify → Insure → Orchestrate → observe result → Determine
```

An orchestration decision is not complete until the resulting state is observed and evaluated again. A requested action is not evidence that it succeeded.

## Shared concepts

The cross-domain vocabulary is intentionally small:

- **Intent**: purpose, success criteria and constraints, with a stable identity/version.
- **State**: timestamped observations and derived values, each tied to a declared schema and source.
- **Desired state**: target conditions and tolerances; it may include ranges, predicates or categorical values, not just normalized numeric coordinates.
- **Finding**: a verifiable comparison result, evidence and confidence/uncertainty.
- **Risk and recovery**: hazards, limits, checkpoint references, recovery options and escalation paths.
- **Workflow and action**: declared steps, preconditions, authorization, idempotency/retry policy, result and transition.

These are semantic responsibilities, not a requirement that every domain be forced into one numeric vector. Numeric scoring can be an optional domain-specific projection. It must not discard units, timestamps, missing-data status, source quality, or hard constraints.

## Domain adapter boundary

A domain adapter translates native systems into and out of the shared contract:

```text
native sensors/APIs ──> adapter ──> state + evidence ──> UPMP pillars
native actuators/APIs <── adapter <── authorized action ── workflow runtime
```

An adapter owns domain schema and units, sensor validation, observation freshness, success predicates, capability discovery and action-specific safety checks. The UPMP core should not need to know what a pump, agent, compiler or service is.

Workflows may contain child workflows and local monitors, but each child has an explicit intent, state scope, authority boundary, completion condition and escalation route. Parent workflows consume child results through the same contract rather than reaching into child internals.

## Safety and authority boundary

Monitoring and orchestration are not substitutes for a safety-rated controller or hardware interlock. For physical systems, local controls must enforce hard limits independently of UPMP, including safe behavior when UPMP, communications, sensors or power fail. Commands need explicit authorization and bounded preconditions; feedback must confirm actual state. A software recommendation alone must never be presented as proof of safe actuation.

In the irrigation example, UPMP can coordinate a schedule, inspect validated power/pump/water observations, and request an approved fallback workflow. A local controller must still enforce electrical protection, generator transfer sequencing, dry-run prevention, pressure limits, emergency stop and safe failure behavior.

Identity, login, credential storage, encryption, compression and key management are external services. UPMP accepts only a caller-supplied opaque principal reference and an injected authorization decision callback. It never receives or stores credentials or keys. This keeps the UPMP boundary focused on process integrity.

## Current implementation and gaps

The repository implements a first closed-loop slice of the contract, with these limits:

- `UPMP/MonitoringEngine` evaluates the legacy numeric state model and emits a report. `UPMP/contracts.py` provides a versioned typed observation envelope, a schema registry for required/optional fields, JSON value types, units, enumerated values and numeric bounds, plus hard equality, membership, range and tolerance predicates. Unit conversion stays with domain adapters. The schema format is a focused Python contract, not a full JSON Schema implementation.
- The current intent-preservation routine uses word matching, not a semantic or policy-grade audit. Trajectory and progress scores are heuristics and must not be interpreted as proof.
- Checkpoint/rollback helpers operate on in-memory monitoring state. They do not restore arbitrary external systems.
- `UPMP/closed_loop.py` integrates precondition verification, allow-listed action dispatch, an optional external authorization callback, result observation, postcondition verification and explicit branching in one bounded loop. Retired shell-runner and monolithic implementations are archived under `legacy/prototypes` and are not supported interfaces.
- The allow-list restricts what the process can dispatch; the optional authorization callback delegates who may dispatch it to an external identity/authorization service. UPMP does not implement authentication, login, credentials, encryption or key storage. Retry requires an idempotent step and reuses an idempotency key, but the handler must enforce deduplication. Handlers run synchronously and cannot be safely cancelled or timed out by this runtime.
- Audit events are returned in memory; durable tamper-evident storage, policy signing/version lifecycle, pause/resume persistence and runtime cancellation are not implemented. Durable audit and identity/key management should remain external integrations, not new UPMP subsystems.
- Recovery can be modeled as explicit workflow branches. `MonitoringEngine` remediation and rollback apply to in-memory monitor state only; they do not restore arbitrary external systems.

## Tightening sequence

The next UPMP improvements should deepen its process-integrity domain:

1. Done: machine-readable state schema validation and richer desired-state predicates, including tolerances/ranges and explicit domain-side unit conversion.
2. Expand workflow semantics with explicit pause/resume snapshots, deadlines and handler-owned cancellation/reconciliation contracts.
3. Make audit event storage pluggable while keeping retention and cryptographic integrity with the external logging/security service.
4. Expand the simulation with unavailable/stale sensors, drift, action failure, recovery, scheduling and resume; keep physical adapters outside the proof until their independent safeguards are validated.

The domain-neutral simulation covers normal scheduled fallback and completion. Focused tests also cover failed checks, unknown data, failed or unauthorized actions, recovery branches, retries, scheduling and step limits. These steps harden one shared architecture; they do not require separate pillars for each domain.

