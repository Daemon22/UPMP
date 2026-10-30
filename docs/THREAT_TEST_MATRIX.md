# UPMP Threat-to-Test Cross-Reference

**Generated**: 2026-10-28 (post-M7.1)
**Method**: Each threat in THREAT.md was mapped to its enforcement location in code and to the specific test that proves the control works. A test "proves the control works" only if it asserts the security property (e.g., `assert verify_chain() is False`), not merely if it exercises the code path.

## Summary

| Threat | Status | Notes |
|---|---|---|
| T1 — Crash recovery | ✅ Implemented and tested | Subprocess kill + parent recovery |
| T2 — Non-terminal tamper | ✅ Implemented and tested | Hash chain detects via prev_hash link |
| T3 — Last-event tamper | ✅ Implemented and tested | Checkpoint catches what chain can't see |
| T4 — Truncation | ✅ Implemented and tested | Checkpoint length mismatch |
| T5 — Out-of-order seq | ✅ Implemented and tested | SQLite PK + verify_chain monotonicity |
| T6 — Stale chain replay | ✅ Implemented and tested | Checkpoint tip mismatch |
| T7 — Sink failure | ✅ Implemented and tested | Fail-loud + append rollback |
| T8 — Resume corruption | ✅ Implemented and tested | Full-chain verify before any modification |
| T9 — Concurrency (concurrent writers) | 🟦 **Accepted by design / out of scope** | Single-writer is a documented contract, not an enforced guarantee; concurrent writers must serialize externally. This is a distinct bucket from "Implemented and tested" — T9 is explicitly out of UPMP's scope. |
| T10 — Disk-write attacker | ✅ Implemented and tested | Transparency log + Merkle root (with documented limitation when attacker controls both files) |
| T11 — Parallel causality | ✅ Implemented and tested | branch_id + lock-serialized appends |

**Note on T9**: T9 is classified "Accepted by design / out of scope" — a distinct bucket from "Implemented and tested." UPMP does not actively enforce single-writer semantics at the sink layer; it documents the single-writer assumption as a contract. Concurrent writers to the same file or database are not detected or rejected at the storage layer; they are expected to serialize externally (e.g., via `flock` or a single-writer service). This is a **design boundary** (single-writer per file is the supported deployment model), not a missing control or a tested guarantee. The M7 `ParallelOrchestrator` serializes audit appends internally via a lock, so concurrent *branches* within a single orchestrator are safe; the T9 limitation applies to *concurrent processes* sharing a storage file.

**Counts**: 10 threats "Implemented and tested" (T1–T8, T10, T11). 1 threat "Accepted by design / out of scope" (T9). 0 threats "Documented only" or "Missing". Total: 11.

## Detailed cross-reference

### T1 — Process crash mid-write loses the in-flight event

- **Claim**: fsync per append + recovery model drops torn final record
- **Enforcement**: `audit.py` `FileAuditSink.append()` (fsync), `load_audit_file(strict=False)` (drops torn tail)
- **Test**: `tests/test_audit_crash.py::TestCrashRecovery::test_child_crashes_mid_write_and_parent_recovers`
- **Proves control works**: Yes — spawns a real subprocess via `os._exit(2)`, parent recovers exactly 3 events, torn tail physically truncated, new append at seq 4 succeeds, `verify_audit_file == True`
- **Classification**: Implemented and tested

### T2 — Tampering with a non-terminal event's payload

- **Claim**: Hash chain detects via broken prev_hash link
- **Enforcement**: `audit.py` `event_hash()`, `InMemoryAuditSink.verify_chain()`
- **Tests**: `tests/test_audit.py::TestTamperDetection` (single_byte, sequence_reorder, event_deletion), `tests/test_audit_storage.py::TestRecoveryModel::test_verify_audit_file_detects_on_disk_tampering`, `tests/test_audit_sqlite.py::TestPrimaryKeyIntegrity::test_out_of_order_seq_detected_by_verify_chain`
- **Proves control works**: Yes — each test mutates an event and asserts `verify_chain() is False` or `verify_audit_file(path) is False`
- **Classification**: Implemented and tested

### T3 — Tampering with the LAST event's payload

- **Claim**: Checkpoint mechanism detects (bare chain cannot)
- **Enforcement**: `audit.py` `write_checkpoint()` / `verify_checkpoint()`
- **Tests**: `tests/test_audit_storage.py::TestTrustedCheckpoint::test_checkpoint_detects_LAST_event_tamper` (proves `verify_chain() is True` — the blind spot — AND `verify_checkpoint() is False` — the catch), `tests/test_audit_sqlite.py::TestCheckpointInterop::test_checkpoint_detects_last_event_tamper_in_sqlite`
- **Proves control works**: Yes — the test explicitly demonstrates the bare chain's blind spot AND the checkpoint's detection
- **Classification**: Implemented and tested

### T4 — Truncation of the chain

- **Claim**: Checkpoint detects via chain_length mismatch
- **Enforcement**: `audit.py` `verify_checkpoint()`
- **Test**: `tests/test_audit_storage.py::TestTrustedCheckpoint::test_checkpoint_detects_truncation`
- **Proves control works**: Yes — truncates the file to 3 events, asserts `verify_checkpoint() is False`
- **Classification**: Implemented and tested

### T5 — Out-of-order, duplicate, or gapped sequence numbers

- **Claim**: SQLite PRIMARY KEY rejects at DB layer; verify_chain checks monotonicity
- **Enforcement**: `audit.py` SQLiteAuditSink schema (PRIMARY KEY), `InMemoryAuditSink.verify_chain()` (sequence check)
- **Tests**: `tests/test_audit_sqlite.py::TestPrimaryKeyIntegrity::test_duplicate_seq_rejected_at_db_layer` (asserts `IntegrityError`), `tests/test_audit.py::TestTamperDetectionExtended::test_sequence_number_mutation_detected`, `tests/test_audit.py::TestTamperDetectionExtended::test_event_duplication_detected`
- **Proves control works**: Yes — each asserts the rejection/detection
- **Classification**: Implemented and tested

### T6 — Substitution of a stale chain (replay)

- **Claim**: Checkpoint tip hash mismatch detects stale substitution
- **Enforcement**: `audit.py` `verify_checkpoint()`
- **Test**: `tests/test_audit_storage.py::TestTrustedCheckpoint::test_checkpoint_detects_reorder`
- **Proves control works**: Yes — substitutes an older chain, asserts `verify_checkpoint() is False`
- **Classification**: Implemented and tested

### T7 — Sink failure during append silently loses the event

- **Claim**: Fail-loud policy + append rollback
- **Enforcement**: `audit.py` `FileAuditSink.append()` rollback, `closed_loop.py` `_record()` propagation
- **Tests**: `tests/test_audit_storage.py::TestAppendFailureRollback::test_failed_append_rolls_back_memory_and_disk` (monkeypatches `os.fsync` to fail), `tests/test_audit_integration.py::TestFailLoudPolicy::test_sink_failure_aborts_run_without_result`, `tests/test_audit.py::TestMalformedEvents` (malformed event doesn't silently pass)
- **Proves control works**: Yes — asserts the run aborts without a WorkflowResult and the partial write is rolled back
- **Classification**: Implemented and tested

### T8 — Resuming a corrupted chain silently accepts bad data

- **Claim**: Resume verifies full chain BEFORE any modification
- **Enforcement**: `audit.py` `FileAuditSink._resume_from_disk()`, `SQLiteAuditSink._resume_from_db()`
- **Tests**: `tests/test_audit_storage.py::TestRecoveryModel::test_resume_refuses_hash_corrupted_chain_and_leaves_file_untouched` (asserts file size unchanged after refusal), `tests/test_audit_sqlite.py::TestResumeRefusesCorruption::test_resume_refuses_hash_corrupted_chain_db_untouched`
- **Proves control works**: Yes — asserts `AuditStorageError` raised AND file/DB untouched (forensic preservation)
- **Classification**: Implemented and tested

### T9 — Concurrent writers interleave events

- **Claim**: Concurrent writers are unsupported; single-writer is a documented contract
- **Enforcement**: Documented in each sink's docstring ("Not thread-safe: one writer per file"). No active detection or rejection of concurrent writers at the storage layer.
- **Test**: No test asserts detection of concurrent writers (there is nothing to detect — the storage layer does not lock). The M7 `ParallelOrchestrator` serializes appends internally via `_audit_lock`, which is tested by `tests/test_parallel.py::TestAuditIntegrity::test_audit_chain_verifies_under_concurrency` — but that test covers concurrent *branches within one orchestrator*, not concurrent *processes sharing a file*.
- **Proves control works**: N/A — T9 is a design boundary, not an enforced guarantee. The single-writer assumption is a contract the caller must honor.
- **Classification**: **Accepted by design / out of scope** (distinct bucket from "Implemented and tested"). The M7 internal lock addresses intra-orchestrator concurrency; inter-process concurrency remains the caller's responsibility.

### T10 — Attacker with disk write access rewrites the chain

- **Claim**: Transparency log (in independent trust domain) catches full-chain rewrite
- **Enforcement**: `audit.py` `TransparencyLog` Protocol, `verify_against_transparency_log()`, `file_transparency_log.py` `FileTransparencyLog` (Merkle root)
- **Tests**: `tests/test_transparency_log.py::TestFileAuditSinkIntegration::test_t10_attack_detected_when_log_independent` (proves `verify_chain() is True` — forged chain looks valid — AND `verify_against_transparency_log() is False` — transparency log catches it), `tests/test_file_transparency_log.py::TestFileAuditSinkIntegration::test_t10_attack_detected_via_file_transparency_log`, `tests/test_file_transparency_log.py::TestFileAuditSinkIntegration::test_t10_both_files_compromised_limitation` (documents the residual risk)
- **Proves control works**: Yes — the key test demonstrates the blind spot (`verify_chain` returns True for a forged chain) and the catch (`verify_against_transparency_log` returns False)
- **Classification**: Implemented and tested (with documented limitation when attacker controls both files)

### T11 — Concurrent branches must not falsify causal order or corrupt the audit chain

- **Claim**: Lock-serialized appends + branch_id provenance + no false success
- **Enforcement**: `parallel.py` `ParallelOrchestrator` (`_audit_lock`, `branch_id` on every event, `step_skipped` for failed deps)
- **Tests**: `tests/test_parallel.py::TestAuditIntegrity::test_branch_id_recorded_on_every_event`, `test_audit_chain_verifies_under_concurrency`, `test_causal_order_preserved_via_branch_id`, `TestDurableStorageConcurrency::test_file_audit_sink_correct_under_concurrency`, `TestFailureIsolation::test_branch_failure_does_not_cancel_unrelated_branch`, `test_dependent_step_skipped_when_prerequisite_fails`, `TestNoFalseSuccess::test_handler_raising_exception_records_failure_not_success`
- **Proves control works**: Yes — each asserts the specific property (branch_id present, chain verifies, no false success, failure isolation)
- **Classification**: Implemented and tested

## M2/M3/M4/M5/M7/External guarantees

| Milestone | Guarantee | Test evidence | Classification |
|---|---|---|---|
| M2 — Cancellation | RunStatus.CANCELLED + run_cancelled event | `test_cancellation.py::TestRunLevelCancellation` (3 tests), `TestPerStepTimeout` (4 tests) | Implemented and tested |
| M2.2 — Thread preemption | Handler preempted within timeout | `test_preemption.py::TestHardPreemption::test_slow_handler_returns_indeterminate_within_timeout` | Implemented and tested (orphan-thread limitation documented) |
| M2.3 — Process preemption | Handler killed via SIGKILL | `test_process_preemption.py::TestHardPreemption::test_infinite_loop_handler_killed`, `TestProcessIsActuallyKilled::test_no_orphan_process_after_preemption` | Implemented and tested |
| M3 — Idempotency | Handler called once per key | `test_idempotency.py::TestDispatcherWithStore::test_retry_returns_cached_result_handler_called_once`, `TestOrchestratorRetryWithStore::test_retry_loop_with_store_invokes_handler_once` | Implemented and tested |
| M4 — HMAC policy signing | Tampered policy fails verification | `test_policy_auth.py::TestHmacPolicyAuthenticator` (12 tests covering 6 tamper dimensions) | Implemented and tested |
| M4.1 — Ed25519 signing | Asymmetric sign/verify + tamper detection | `test_asymmetric_policy_auth.py::TestTamperDetection` (6 tests), `TestWrongKeyPair` | Implemented and tested |
| M5 — Evidence completeness | Missing required fields detected | `test_evidence.py::TestValidatorMissingFields` (6 tests), `TestEndToEndWithOrchestrator::test_tampered_evidence_detected` | Implemented and tested |
| M7 — Parallel DAG | Independent branches run concurrently | `test_parallel.py::TestConcurrentExecution::test_independent_branches_execute_concurrently` (overlap verified via timestamps) | Implemented and tested |
| M7.1 — Transitions | on_success/on_failure/on_indeterminate routing within branches | `test_parallel_transitions.py` (25 tests covering all 9 required scenarios + self-transition rejection) | Implemented and tested |
| External — Transparency log | T10 closure when log in independent trust domain | `test_transparency_log.py`, `test_file_transparency_log.py` (41 tests total) | Implemented and tested |

## Findings

**10 of 11 threats are "Implemented and tested"** (T1–T8, T10, T11) — each has at least one test that proves the control works by asserting the security property (not merely exercising the code).

**1 threat is "Accepted by design / out of scope"** (T9) — UPMP does not actively enforce single-writer semantics at the storage layer; it documents the single-writer assumption as a contract that callers must honor. This is a distinct classification bucket, not a soft claim of "implemented." A reader of this matrix should understand that concurrent processes sharing an audit file is NOT a scenario UPMP detects or prevents — it is the caller's responsibility to serialize externally.

**0 threats classified as "Documented only" or "Missing".**

**No test count inflation.** The 363 tests cover real security properties. The number of tests does not by itself prove completeness; the cross-reference above is the evidence that each threat is genuinely covered.

**Residual limitations (documented, not defects):**
- T9: concurrent writers not supported at the storage layer (single-writer contract; intra-orchestrator concurrency handled by M7's internal lock)
- T10: attacker controlling both chain file AND transparency log defeats detection (requires external trust anchor)
- M2.2: preempted handler threads keep running (CPython cannot kill threads; use M2.3 for hard kill)
- M7: audit chain order under concurrency is non-deterministic across runs (verifiable within a run)
- M7.1: self-transitions (on_success='a' for step a) rejected at DAG validation (use max_retries for retry loops)
