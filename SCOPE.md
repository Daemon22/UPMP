# UPMP Scope and Integration-Boundary Specification

**Status**: Draft 1.0 — adopted as the authoritative scope document for UPMP.
**Date**: 2026-10-28

This document defines UPMP's position within the foundation, what it owns,
what it does not own, and the rules that enforce ownership boundaries.
Any proposed UPMP feature that conflicts with another platform's ownership
must trigger a boundary review before implementation.

---

## 1. Architectural objective

The foundation consists of multiple independent platforms that may collaborate
to achieve larger objectives. Each platform must retain a distinct purpose,
own its domain, and avoid recreating capabilities already provided elsewhere.

The architecture must satisfy four requirements:

- **Clear ownership**: Every capability has a designated owner.
- **Independence**: Every platform can perform its intended role without
  requiring the other platforms to be installed.
- **Interoperability**: Platforms can communicate through defined interfaces
  when collaboration is necessary.
- **No unnecessary duplication**: A platform may coordinate or invoke another
  platform's capability without independently rebuilding that capability.

The goal is not to force everything into one application or codebase. It is to
create an ecosystem of autonomous platforms that can operate separately and
cooperate when appropriate.

## 2. Where UPMP belongs

**UPMP — Workflow Orchestration and Process Assurance**

UPMP is the foundation's general-purpose process execution and orchestration
engine. It determines how a multi-step process should execute, what conditions
must be satisfied, how its steps depend on one another, what happens when a
step fails, and whether the overall process has succeeded.

It manages the execution of a process. It does not need to own the tools that
perform every operation within that process.

### UPMP owns

- Workflow definitions and execution.
- Dependency graphs and transition-based routing.
- Sequential and parallel process execution.
- Preconditions, postconditions, and execution policies.
- Workflow state and execution-state transitions.
- Retry and recovery policies.
- Failure handling and cancellation propagation.
- Prevention of duplicate step execution.
- Process-level audit records and execution evidence.
- Reporting whether the overall process completed, failed, or was blocked.
- Generic integration interfaces through which external capabilities can be
  invoked.

### UPMP does not own

UPMP should not become a second secrets manager, storage platform, browser
automation system, software-generation engine, collaboration platform, or
operating environment. Specifically, UPMP should not independently implement:

- Credential storage and secret rotation.
- General-purpose artifact storage and archival.
- A complete software-development and project-generation environment.
- Browser sessions, browser automation, and web interaction.
- Manya's collaboration and community capabilities.
- Operating-system-level application management.
- A universal graphical interface for every foundation platform.
- Independent copies of tools already maintained by another platform.

**Ownership principle**: UPMP owns the process that connects capabilities.
The platform responsible for each capability owns its implementation.

## 3. The platform responsibility map

### 3.1 Keeper of Secrets — Secret and Credential Authority
Domain: sensitive credentials, secrets, and controlled access.
UPMP may request a credential to perform a process, but this does not make
credential management an UPMP responsibility. UPMP should request authorized
access through a supported interface or pass a reference the authorized
component can resolve.

### 3.2 Vault — General Storage and Artifact Authority
Domain: persistent storage of non-secret information, files, and artifacts.
UPMP should store references to important artifacts in its runtime state
rather than maintaining its own competing artifact repository.

### 3.3 Craft Engine — Creation and Construction Authority
Domain: building, generating, and modifying projects.
Craft Engine owns construction; UPMP owns cross-capability process coordination.

### 3.4 Browser — Web Interaction and Browsing Authority
Domain: browser-based access, navigation, and interaction.
UPMP should not recreate browser automation inside its own scheduler.

### 3.5 Manya — Collaboration and Shared-Activity Platform
Domain: human collaboration, shared work, and coordinated participation.
UPMP should not become a second Manya interface, shared workspace, community
platform, or collaboration database.

### 3.6 Manya OS — System and Application Environment
Domain: the operating environment through which supported applications and
platform capabilities are made available and managed. (Provisional pending
audit of actual implementation.)

## 4. Consolidated ownership matrix

| Capability | Primary owner | UPMP's role |
|---|---|---|
| Workflow execution | UPMP | Owns |
| Dependency and transition management | UPMP | Owns |
| Preconditions, postconditions, and workflow policies | UPMP | Owns |
| Retries, recovery, and cancellation within workflows | UPMP | Owns |
| Process-level execution records | UPMP | Owns |
| Secret storage and lifecycle | Keeper of Secrets | Requests authorized access |
| Secret authorization | Keeper of Secrets | Supplies workflow context to request access |
| General persistent artifacts | Vault | Coordinates storage and retrieval |
| Artifact construction and software generation | Craft Engine | Invokes the construction capability |
| Build operations | Craft Engine | Coordinates within larger workflows |
| Browser sessions and web interaction | Browser | Invokes the browser capability |
| Collaboration workspaces and participation | Manya | Coordinates processes involving collaboration |
| Operating environment and app lifecycle | Manya OS | Runs independently or integrates when appropriate |
| Cross-platform process coordination | UPMP | Owns |
| Domain-specific business logic | Domain owner | Invokes when needed |
| Cross-platform integration adapters | Separate integration components | Supports via stable interfaces |

## 5. How the platforms should work together

Integration can use an in-process interface, a command-line adapter, a local
service, an API, or another supported transport. The critical requirement is
that the communication boundary is explicit and that the core package does
not depend on every integration being present.

## 6. Independence must be preserved

Integration capability must never become a mandatory dependency. UPMP should
remain a fully usable process engine even when none of the other foundation
platforms are installed or available.

**Dependency direction**:
```
Foundation platform → Optional UPMP adapter → UPMP integration interface → UPMP core
```
The core should not import Keeper of Secrets, Vault, Craft Engine, Browser,
Manya, or Manya OS directly. The absence of an adapter should mean that a
particular integration is unavailable, not that UPMP itself is unusable.

## 7. The integration contract

Each externally provided capability should have:
- A stable identifier and declared version (capability identity).
- Defined request/response schemas (input and output).
- A structured outcome distinguishing: success, failure, condition preventing
  execution, cancellation, timeout, unavailable capability.
- No silent authority escalation; credentials obtained through the appropriate
  authority, never copied into general workflow state or process logs.
- Defined timeout and cancellation behavior; a timeout must not be treated as
  proof the external operation stopped.
- Declared idempotency/retry safety; UPMP coordinates retry policies but
  cannot assume every external operation is safe to repeat.
- Process-level audit evidence (step, requested capability, observed outcome,
  result references); the provider retains its own domain-specific audit;
  sensitive values excluded from ordinary execution evidence.

## 8. Preventing duplicate functionality

Governance rule for every new capability:
1. **Existing capability**: use the existing owner's implementation.
2. **Missing integration**: build an adapter if the capability exists but
   cannot yet be invoked through UPMP.
3. **Missing capability**: ask the appropriate domain owner to implement it.
4. **Genuinely cross-domain requirement**: implement generic coordination in
   UPMP when the requirement concerns process execution.
5. **Unclear ownership**: resolve the boundary before creating another
   implementation.

## 9. Architectural rules to enforce in UPMP

1. **Standalone operation**: the core test suite must run without the other
   foundation platforms installed or available.
2. **No platform-specific dependency in the core**: platform-specific
   integration code must remain outside UPMP's core execution modules.
3. **Optional integrations**: an absent integration must be reported clearly
   and must not prevent unrelated workflows from executing.
4. **No duplicate domain engines**: UPMP must not accumulate its own
   independent implementations of secrets management, general artifact
   storage, browser automation, construction tooling, collaboration, or
   operating-system management.
5. **Generic process semantics remain in UPMP**: dependencies, transitions,
   conditions, workflow recovery, and process-level execution outcomes must
   remain defined by UPMP's own execution contract.
6. **Domain logic stays with its owner**: where a provider has its own
   internal process, UPMP should invoke that process as one capability
   rather than duplicating its internal steps.
7. **Explicit data ownership**: workflow state must not become an alternative
   permanent database for Vault, Keeper of Secrets, Manya, or another domain
   owner.
8. **Observable failures**: a provider or adapter failure must produce a
   meaningful execution outcome; an unavailable capability must not be
   reported as a successfully completed step.
9. **Independent versioning**: integrations should be versioned and tested
   separately where practical.
10. **Evidence-based release decisions**: the test suite should demonstrate
    that UPMP still operates independently and that its integration boundaries
    remain intact.

## 10. Immediate scope decisions

- **Keep in the core**: process scheduling, dependency management, execution
  policies, transitions, condition evaluation, retries and recovery,
  cancellation, workflow state, and execution evidence.
- **Keep outside the core**: platform-specific clients, secret retrieval
  implementations, general artifact repositories, browser automation,
  construction tools, collaboration interfaces, and OS integrations.
- **Define now, implement later**: the generic capability-provider contract,
  adapter lifecycle, capability metadata, request/result schemas, timeout and
  cancellation behavior, and execution-evidence rules.
- **Do not implement merely for future compatibility**: direct integrations
  with every other platform. Extension points are provided; adapters are built
  only when there is a concrete use case and a verified interface.
- **Audit before freezing the wider map**: review the actual responsibilities
  and interfaces of Keeper of Secrets, Vault, Craft Engine, Browser, Manya,
  and Manya OS against this ownership model.

## 11. Final architectural decision

UPMP is an independent workflow orchestration and process-assurance engine.
It coordinates multi-step operations, manages execution dependencies and
outcomes, enforces workflow policies, and records execution evidence. It does
not own the domain-specific tools or services that perform the operations
within those workflows. It remains independently usable while supporting
optional, explicitly defined integrations with other foundation platforms.
