# UPMP Threat Model

**Status**: Living document. Last reviewed 2026-10-28 against the
post-M1.7 source tree (`UPMP/audit.py`, `UPMP/closed_loop.py`).
Findings cited by file:line where applicable.

**Scope**: This document defines what UPMP's audit subsystem is
designed to defend against, what it explicitly does **not** defend
against, and which controls deliver each guarantee. It is the
authoritative reference when a security claim about UPMP audit is
in question. If a docstring or comment in the code disagrees with
this document, this document wins (and the code should be fixed, not
the document).

---

## 1. Assets

| Asset | Description | Where it lives |
|---|---|---|
| **A1 — Audit chain integrity** | The sequence of `AuditEvent` records produced by a workflow run, including their `prev_hash` links and `sequence_number` monotonicity. | `FileAuditSink`: `*.jsonl`. `SQLiteAuditSink`: `*.db`. `InMemoryAuditSink`: process memory. |
| **A2 — Audit chain authenticity** | The property that the chain on disk is the one that was actually produced by the trusted writer process, not a forgery. | The chain file itself; an independent trust anchor (checkpoint file, external log). |
| **A3 — Audit availability** | The property that a recorded event survives process exit, crash, or restart. | The storage medium (disk + fsync for `FileAuditSink`; SQLite WAL for `SQLiteAuditSink`). |
| **A4 — Audit ordering** | The property that `sequence_number` is monotonically increasing without gaps or duplicates. | The DB PRIMARY KEY for `SQLiteAuditSink`; the in-memory list append for `InMemoryAuditSink`; the file's append-only-by-convention discipline for `FileAuditSink`. |
| **A5 — Workflow execution integrity** | The property that the orchestrator's `WorkflowResult.audit` matches what was actually executed (every dispatched action recorded, every recorded event reflects a real dispatch). | `ClosedLoopOrchestrator._record()` wiring (`closed_loop.py:212`). |

---

## 2. Adversaries

### 2.1 In scope

| Adversary | Capability | Why in scope |
|---|---|---|
| **Adv-C1 — Crash** | The writer process is killed (SIGKILL, power loss, OOM) at any point between two `append()` calls or during one. | Realistic operating environment; UPMP makes no resilience assumptions about its host. |
| **Adv-C2 — Single-event tamper** | An attacker (or a buggy tool) modifies the `details` / `event` / `step_id` / `timestamp` field of one or more **non-terminal** records in the chain file or DB, without updating `prev_hash` links. | Realistic misconfiguration, log-rotation accident, or partial-disk-corruption scenario. |
| **Adv-C3 — Truncation** | An attacker or accident truncates the chain file or DB to a prefix of its previous length. | Realistic for log rotation, disk-full events, partial file copies. |
| **Adv-C4 — Out-of-order insertion** | An attacker inserts a record whose `sequence_number` is out of order, duplicated, or has a gap. | The `SQLiteAuditSink` schema defends at the DB layer (`PRIMARY KEY`); `FileAuditSink` defends via `verify_chain()`; both paths are in scope. |
| **Adv-C5 — Replay of stale chain** | An attacker substitutes an older version of the chain file for the current one (e.g. restoring from a stale backup to hide recent events). | Realistic operational accident. Detectable via the checkpoint mechanism when the checkpoint is in an independent trust domain. |

### 2.2 Out of scope

| Adversary | Capability | Why out of scope |
|---|---|---|
| **Adv-X1 — Attacker with disk write access** | Can modify, truncate, or replace the chain file **and** the checkpoint file. | Defeating this requires OS-level append-only flags (`chattr +a` on Linux), WORM storage, or an external transparency log with an external verifier. UPMP documents this gap but does **not** close it. Production deployments requiring protection against this adversary must integrate an external trust anchor. |
| **Adv-X2 — Full-chain rewrite** | An attacker who can write the chain file can recompute every `prev_hash` consistently and produce a valid-looking chain from scratch. | Same remedy as Adv-X1: requires a signed genesis hash, a trusted timestamping authority, or a transparency log. Out of scope for UPMP core per `ARCHITECTURE.md` §"Safety and authority boundary". |
| **Adv-X3 — Network attacker** | Can intercept or modify network traffic between UPMP and any external service (authorization, storage, transparency log). | UPMP makes no network security claims. Network security is the deployment's responsibility. |
| **Adv-X4 — Supply-chain attacker** | Can modify UPMP itself, its dependencies, or the Python interpreter. | Out of scope; verify provenance via reproducible builds and signed releases (not yet implemented; tracked as a future hardening item). |
| **Adv-X5 — Hardware attacker** | Can read or modify memory via DMA, cold-boot, or rowhammer. | Out of scope; requires hardware-level defenses (TEE, IOMMU, memory encryption). |
| **Adv-X6 — Concurrency / TOCTOU** | Two writers append to the same chain file concurrently. | UPMP audit sinks are explicitly **not thread-safe** (documented in each sink's docstring). One writer per file is the supported deployment. |

---

## 3. Threats and controls

Each threat maps to one or more controls and to the tests that prove the control works.

### T1 — Process crash mid-write loses the in-flight event

- **Asset**: A3 (availability), A1 (integrity — the file may end with a partial line)
- **Adversary**: Adv-C1
- **Control (FileAuditSink)**: `append()` calls `flush()` + `os.fsync()` before returning (`audit.py:683-685`). A committed event is on disk; an uncommitted event is not. The recovery model (`load_audit_file(strict=False)`) drops a single torn final record; non-final malformed records always raise `AuditStorageError`.
- **Control (SQLiteAuditSink)**: each `append()` runs inside a `with self._conn:` transaction block (`audit.py:851-858`); SQLite's WAL + `synchronous=NORMAL` flushes to disk on COMMIT. An uncommitted transaction is rolled back by SQLite on next open.
- **Tests**: `tests/test_audit_crash.py::TestCrashRecovery::test_child_crashes_mid_write_and_parent_recovers` (subprocess killed via `os._exit(2)`, parent recovers exactly the 3 fsync'd events); `tests/test_audit_storage.py::TestRecoveryModel::test_crash_recovery_drops_single_torn_final_record`.

### T2 — Tampering with a non-terminal event's payload

- **Asset**: A1, A2
- **Adversary**: Adv-C2
- **Control**: each event's `prev_hash` stores the SHA-256 of the preceding event's payload (`audit.py:144-158`). `verify_chain()` recomputes the chain and compares; a mismatch on event N is detected when event N+1's `prev_hash` no longer matches the recomputed self-hash of event N.
- **Tests**: `tests/test_audit.py::TestTamperDetection` (single-byte tamper, reorder, deletion, duplication, sequence mutation); `tests/test_audit_storage.py::TestRecoveryModel::test_verify_audit_file_detects_on_disk_tampering`; `tests/test_audit_sqlite.py::TestPrimaryKeyIntegrity::test_out_of_order_seq_detected_by_verify_chain` (SQL `UPDATE` of the `line` column).

### T3 — Tampering with the LAST event's payload

- **Asset**: A1, A2
- **Adversary**: Adv-C2
- **Control**: the bare hash chain **cannot** detect this (the last event has no successor). Detection is provided by `write_checkpoint()` / `verify_checkpoint()` (`audit.py:507-573`), which commits to the chain's tip hash and length. Recomputing the chain after tampering and comparing against the stored tip detects the modification — **provided the checkpoint file lives in an independent trust domain** (out-of-band copy, externally signed blob, another host). An attacker controlling both files can recompute the chain and write a matching checkpoint; the checkpoint provides no protection in that case.
- **Tests**: `tests/test_audit_storage.py::TestTrustedCheckpoint::test_checkpoint_detects_LAST_event_tamper` (proves `verify_chain()` returns True after last-event tampering — the blind spot — while `verify_checkpoint()` returns False); `tests/test_audit_sqlite.py::TestCheckpointInterop::test_checkpoint_detects_last_event_tamper_in_sqlite`.

### T4 — Truncation of the chain

- **Asset**: A1, A3
- **Adversary**: Adv-C3
- **Control**: `verify_checkpoint()` compares the stored `chain_length` against the recomputed chain length. A truncated chain has a smaller length → mismatch → False.
- **Tests**: `tests/test_audit_storage.py::TestTrustedCheckpoint::test_checkpoint_detects_truncation`.

### T5 — Out-of-order, duplicate, or gapped sequence numbers

- **Asset**: A4
- **Adversary**: Adv-C4
- **Control (FileAuditSink)**: `verify_chain()` checks `event.sequence_number != i + 1` for each event in the verified range (`audit.py:293-294`).
- **Control (SQLiteAuditSink)**: the `seq INTEGER PRIMARY KEY` constraint makes duplicate or out-of-order inserts raise `sqlite3.IntegrityError` at the DB layer (`audit.py:806-814`). This is a **storage-layer** defense — the bad insert never lands — whereas `FileAuditSink` defends at the **verification** layer (the bad data lands but is detected on the next `verify_chain()` call).
- **Tests**: `tests/test_audit_sqlite.py::TestPrimaryKeyIntegrity::test_duplicate_seq_rejected_at_db_layer`; `tests/test_audit.py::TestTamperDetectionExtended::test_sequence_number_mutation_detected`; `tests/test_audit.py::TestTamperDetectionExtended::test_event_duplication_detected`.

### T6 — Substitution of a stale chain (replay)

- **Asset**: A2
- **Adversary**: Adv-C5
- **Control**: `verify_checkpoint()` compares the stored tip hash against the recomputed tip. A stale chain has a different tip → mismatch → False.
- **Tests**: `tests/test_audit_storage.py::TestTrustedCheckpoint::test_checkpoint_detects_truncation` (subsumes the stale-replay case: the checkpoint's stored length and tip both fail to match).

### T7 — Sink failure during append silently loses the event

- **Asset**: A5
- **Adversary**: Adv-C1 (crash during fsync), or hardware/disk-full
- **Control**: `FileAuditSink.append()` best-effort rolls back (truncates the partial line + pops the in-memory copy) and re-raises (`audit.py:686-694`). `SQLiteAuditSink.append()` rolls back the in-memory copy on any exception (`audit.py:859-861`). The orchestrator's **fail-loud policy** propagates the exception: `run()` aborts without returning a `WorkflowResult` (`closed_loop.py:166-172`), so the framework cannot report a completed run whose evidence trail is silently incomplete.
- **Tests**: `tests/test_audit_storage.py::TestAppendFailureRollback::test_failed_append_rolls_back_memory_and_disk` (monkeypatches `os.fsync` to fail mid-append); `tests/test_audit_integration.py::TestFailLoudPolicy::test_sink_failure_aborts_run_without_result`.

### T8 — Resuming a corrupted chain silently accepts bad data

- **Asset**: A1, A2
- **Adversary**: Adv-C2 (post-tamper resume)
- **Control**: both `FileAuditSink._resume_from_disk()` and `SQLiteAuditSink._resume_from_db()` verify the FULL chain **before** any modification of the file/DB. A corrupted chain raises `AuditStorageError` and the storage is left untouched for forensic inspection (`audit.py:658-661`, `audit.py:835-838`).
- **Tests**: `tests/test_audit_storage.py::TestRecoveryModel::test_resume_refuses_hash_corrupted_chain_and_leaves_file_untouched`; `tests/test_audit_sqlite.py::TestResumeRefusesCorruption::test_resume_refuses_hash_corrupted_chain_db_untouched`.

### T9 — Concurrent writers interleave events

- **Asset**: A1, A4
- **Adversary**: Adv-X6
- **Control**: **none** — explicitly documented as out of scope ("Not thread-safe: one writer per file" in each sink's docstring). Concurrent writers must serialize externally (e.g. via `flock` on the file or a single-writer service).
- **Tests**: none (out of scope by design).

### T10 — Attacker with disk write access rewrites the chain

- **Asset**: A1, A2
- **Adversary**: Adv-X1, Adv-X2
- **Control**: **none within UPMP**. The hash chain provides detection only when the attacker cannot also rewrite the verification anchor (the checkpoint, or an external transparency log). An attacker who controls the storage can produce a valid-looking chain + matching checkpoint.
- **Tests**: documented limitation; `tests/test_audit_storage.py::TestTrustedCheckpoint` tests prove detection works when the checkpoint is independent, but do not (and cannot) prove detection when the attacker controls both files.

---

## 4. Control summary

| Control | Mechanism | Threats mitigated | Residual risk |
|---|---|---|---|
| SHA-256 hash chain (`prev_hash` field) | Each event stores the hash of the previous event's payload; tampering event N breaks event N+1's `prev_hash` link | T2, T5 (partly), T8 | Cannot see T3 (last event); cannot defend T10 |
| `sequence_number` monotonicity | `verify_chain()` checks `seq == i + 1` for each event | T5 | None for the verification layer; `SQLiteAuditSink` PRIMARY KEY is a stronger storage-layer defense |
| `fsync` per append | Each `append()` flushes + fsyncs before returning | T1, T7 (durability half) | Does not make the file append-only or tamper-proof (T10) |
| SQLite WAL + `synchronous=NORMAL` | ACID transaction per `append()` | T1, T7 (durability half) | Same residual as fsync |
| Recovery model (`load_audit_file(strict=False)`) | Drops a single torn final record; raises on non-final malformed records | T1 | Cannot distinguish torn-write from deliberate corruption; forensic mode (strict=True) raises either way |
| Resume-time full-chain verification | `verify_chain()` runs before any truncation or new append | T8 | None; the chain is verified or the resume is refused |
| Trusted checkpoint (`write_checkpoint` / `verify_checkpoint`) | Commits to chain tip hash + length in a separate file | T3, T4, T6 | Effective only if the checkpoint file is in an independent trust domain |
| PRIMARY KEY constraint (`SQLiteAuditSink` only) | DB rejects duplicate `seq` at insert time | T5 (storage layer) | Does not prevent `UPDATE` of existing rows (the hash chain catches that) |
| Fail-loud policy on sink failure | Orchestrator's `_record()` lets `append()` exceptions propagate; `run()` aborts without `WorkflowResult` | T7 | Callers must handle the exception; events already accepted remain recorded |
| Append-rollback on failure | `FileAuditSink` truncates partial line + pops in-memory copy; `SQLiteAuditSink` pops in-memory copy | T7 (atomicity half) | Best-effort on `FileAuditSink` (if `os.truncate` also fails, the file may have a torn line; recovery model handles this on next open) |

---

## 5. Trust model

### 5.1 Writer trust

- **Assumed**: a single trusted writer process appends to the chain. The writer is the only entity allowed to call `sink.append()`.
- **Not assumed**: the writer never crashes (T1 handles this), the writer never produces malformed events (T7 handles this), the writer never appends out of order (T5 handles this).

### 5.2 Storage trust

- **Assumed**: the storage medium (disk) does not silently corrupt data without also failing reads. SQLite's checksums and the hash chain itself provide detection for the rare cases where this assumption is violated.
- **Not assumed**: the storage medium is tamper-proof. An attacker with disk write access can modify or replace the file/DB (T10). Detection requires an independent trust anchor (checkpoint, transparency log).

### 5.3 Checkpoint trust

- **Assumed** (for T3, T4, T6 mitigation): the checkpoint file is stored in a trust domain the attacker cannot write to. Examples: out-of-band copy to a separate host, externally signed blob, append-only transparency log.
- **Not assumed**: the checkpoint is signed. UPMP does not implement digital signatures; no key material is involved. The checkpoint is a plain JSON file whose authenticity depends entirely on where it is stored.

### 5.4 Out of scope

- **Authentication**: UPMP does not authenticate the writer. Callers must inject an `AuthorizationCheck` callback at the dispatcher (`closed_loop.py:117`) for action-level authorization; audit-write authorization is the deployment's responsibility.
- **Encryption**: UPMP does not encrypt the chain file or the checkpoint file. Encryption at rest is the deployment's responsibility.
- **Network security**: UPMP makes no network security claims.
- **Hardware security**: UPMP does not defend against hardware attackers (Adv-X5).

---

## 6. Mapping to milestones

| Milestone | Status | Threats addressed | Notes |
|---|---|---|---|
| M1.1 — AuditSink + InMemoryAuditSink + hash chain | Done | T2 (detection), T5 (verification layer) | Foundation; in-memory only |
| M1.2 — Range verification, malformed-event handling, security claims corrected | Done | T2 (refined), T8 (malformed events don't crash verifier) | Smallest safe first step |
| M1.3 — FileAuditSink + recovery model + trusted checkpoint | Done | T1, T3, T4, T6, T7, T8 | First durable storage; closes last-event blind spot via checkpoint |
| M1.4 — SQLiteAuditSink | Done | T5 (storage layer via PRIMARY KEY), T1 (ACID) | Alternative durable backend |
| M1.5 — Orchestrator integration | Done | T7 (fail-loud), A5 (every event recorded) | Audit evidence part of real lifecycle |
| M1.6 — Persisted-chain tamper tests | Done (folded into test_audit_storage.py + test_audit_sqlite.py) | T2, T3, T8 | Not a separate milestone file; covered by M1.3/M1.4 tests |
| M1.7 — Crash-recovery test | Done | T1 (end-to-end subprocess crash) | Closes the loop on T1 |
| M1.8 — README limits update | Done | (Documentation) | Removes the in-memory caveat |
| M0 — THREAT.md + CI | This document + `.github/workflows/ci.yml` | (Process) | Defines the trust model the rest of M1.x assumes |

### Deferred

| Milestone | Threats to address | Status |
|---|---|---|
| M2 — Cancellation / timeout | T-NEW: long-running handler cannot be interrupted; no deadline enforcement | Not started |
| M3 — Enforced idempotency | T-NEW: retried action with non-idempotent handler can cause duplicate side effects | Not started |
| M4 — Policy signing / version lifecycle | T-NEW: `VerificationPolicy` cannot be authenticated; a tampered policy could pass anything | Not started |
| M5 — Evidence completeness | T-NEW: an event's `details` may omit fields that the policy expected | Not started |
| M6 — End-to-end adversarial demo | (Demonstrates T1–T10 in a runnable demo) | Not started |
| External — Transparency log integration | T10 (full-chain rewrite protection) | Out of UPMP core scope; deployment responsibility |

---

## 7. Change history

| Date | Change | Author |
|---|---|---|
| 2026-10-28 | Initial THREAT.md (post-M1.7, post-M0). Reflects UPMP 2.3.0 + M1.1–M1.8 audit subsystem. | Z.ai (M0) |
| 2026-10-28 | Added T11 (parallel causality) for M7 — ParallelOrchestrator with concurrent branches. | Z.ai (M7) |
| 2026-10-28 | M7.1 — per-step transition routing within parallel branches (on_success/on_failure/on_indeterminate). Cross-branch routing rejected at DAG validation. | Z.ai (M7.1) |


## 8. Parallel execution (M7)

### T11 — Concurrent branches must not falsify causal order or corrupt the audit chain

- **Asset**: A1 (chain integrity), A5 (workflow execution integrity)
- **Adversary**: Adv-C1 (crash mid-parallel-run), Adv-C2 (single-event tamper in a merged trail)
- **Control (chain integrity under concurrency)**: all `audit_sink.append()` calls from concurrent worker threads are serialized under a `threading.Lock` held by `ParallelOrchestrator`. The chain order is "real-time append order" — non-deterministic across runs but consistent within a run, and `verify_chain()` succeeds. Concurrent appends cannot corrupt the `prev_hash` links because the lock ensures only one thread computes the next hash at a time.
- **Control (causal provenance)**: every `AuditEvent` carries `branch_id` (M7) recording which step it came from. A reader of the merged audit trail can reconstruct causality by filtering on `branch_id`. The flat `WorkflowResult.audit` list is in real-time append order (a real, defensible property — "these events were appended in this order"); it does NOT falsely imply that branch A's events causally preceded branch B's events. Each event's `branch_id` is hash-covered (part of `canonical_json`) so tampering with it is detectable.
- **Control (failure isolation)**: a step whose dependency did not complete successfully is SKIPPED (audit event `step_skipped` with `dependency` and `dependency_status` fields). The framework never invents a successful outcome for skipped work. Unrelated branches continue independently.
- **Control (cancellation)**: if the cancellation token fires, all in-flight futures are cancelled at the executor level. Steps that returned INDETERMINATE due to the token are recorded as `step_cancelled` (not `step_failed`), so the run's final status is `CANCELLED`.
- **Tests**: `tests/test_parallel.py::TestAuditIntegrity` (3 tests: branch_id on every event, chain verifies under concurrency, causal order via branch_id filter), `TestDurableStorageConcurrency` (FileAuditSink correct under concurrent branches), `TestFailureIsolation` (branch failure doesn't cancel unrelated branches, dependent skipped when prereq fails), `TestCancellationPropagation` (cancellation propagates to in-flight branches).

### T11 — Limitations (documented)

- The audit chain order under concurrency is non-deterministic across runs. Two runs of the same parallel workflow may produce different chain orders (depending on thread scheduling). The chain is still verifiable; only the ORDER differs. For deterministic ordering, callers can post-process by sorting on `(branch_id, sequence_number)` — but the chain's hash links are computed in real-time order and must be verified in that order.
- `ThreadPoolExecutor` is subject to the GIL. Suitable for IO-bound handlers (the typical "process" case). For CPU-bound parallelism, wrap handlers in `ProcessPreemptibleDispatcher` (M2.3) — each handler internally spawns a subprocess.
- `InMemoryIdempotencyStore` is not thread-safe. `ParallelOrchestrator` locks around dispatch when a store is configured, serializing dispatches. For truly concurrent dispatch with a shared store, provide a thread-safe store implementation.
- Worker threads that ignore the cancellation token and never return cannot be preempted by `ParallelOrchestrator`. Wrap the dispatcher in `ProcessPreemptibleDispatcher` (M2.3) for hard preemption per step.
