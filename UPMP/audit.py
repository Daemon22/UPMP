"""Hash-chained audit sink for UPMP workflow events.

M1.1 scope — defines the AuditSink protocol and a default in-memory
implementation with SHA-256 hash chaining. This module is purely additive:
no existing UPMP module imports it, and the only change to closed_loop.py
is two backward-compatible fields on AuditEvent (sequence_number, prev_hash).

M1.2 scope — hardens verification: implements start_seq range verification,
adds exception handling for malformed events, corrects security claims,
and adds 13 new tests covering first/last event tamper, duplication,
sequence-number mutation, nested dicts, non-JSON values, range
verification, and malformed-event rejection.

Roadmap:
  M1.1 (this file)  — AuditSink Protocol + InMemoryAuditSink + hash chain
  M1.2 (this file)  — range verification, malformed-event handling, corrected
                       security claims, expanded test coverage
  M1.3 (this file)  — FileAuditSink (JSONL + fsync), recovery model
                       (load_audit_file), trusted chain checkpoint
                       (write_checkpoint / verify_checkpoint)
  M1.4 (this file)  — SQLiteAuditSink (single-file SQLite, PRIMARY KEY
                       enforces monotonic seq at DB level)
  M1.5 (closed_loop.py) — Wire ClosedLoopOrchestrator to call sink.append()
                       per event
  M1.6              — Tamper-detection test against persisted chain
  M1.7              — Crash-recovery test (kill mid-run, reload, verify chain)
  M1.8              — Update README 'Current limits' to remove in-memory caveat

Hash-chain construction:
  - AuditEvent gains two optional fields: sequence_number (int, default 0)
    and prev_hash (str, default ""). These are assigned by the sink at
    append() time and are excluded from the hash payload.
  - The "self hash" of event N is:
      event_hash(event_N.prev_hash, event_N)
      = SHA-256(event_N.prev_hash || NUL || canonical_json(payload_N))
    where payload_N = {event, step_id, timestamp, details}.
  - event_{N+1}.prev_hash stores event_N's self hash.
  - event_1.prev_hash = GENESIS_HASH = "" (empty string).
  - canonical_json uses sort_keys=True so field insertion order does not
    affect the hash. Non-serializable values are coerced via default=str.

Security properties and limitations (READ BEFORE DEPLOYING):

  WHAT THE HASH CHAIN PROVIDES (Verified by tests):
  - Detection of tampering with ANY event EXCEPT the last one. Mutating
    event N's payload (event/step_id/timestamp/details) changes its
    recomputed self-hash, which no longer matches event N+1's prev_hash.
    Detected by verify_chain().
  - Detection of event reordering (breaks both prev_hash links and
    sequence_number monotonicity).
  - Detection of event deletion (breaks the prev_hash link — the gap
    is detectable).
  - Detection of event duplication (sequence numbers become non-monotonic).
  - Detection of sequence_number mutation (breaks monotonicity check).

  WHAT THE HASH CHAIN DOES NOT PROVIDE (Unresolved — out of scope for M1.x):
  - It does NOT detect tampering with the LAST event in the chain. The
    last event has no successor whose prev_hash would catch the mismatch.
    Detecting last-event tampering requires an external anchor (a signed
    checkpoint or transparency-log entry that commits to the last event's
    hash). This is out of scope for M1.x and must be provided by an
    external security service per ARCHITECTURE.md's security boundary.
  - It does NOT protect against an attacker who can rewrite the ENTIRE
    chain. Such an attacker can recompute all prev_hash values consistently
    and produce a valid-looking chain. Authenticity against this threat
    requires an external trust anchor (a signed genesis hash, a trusted
    timestamping authority, or a transparency log with an external
    verifier) that the attacker cannot forge. This is out of scope for
    UPMP core.

  WHAT PERSISTENT STORAGE (M1.3) AND fsync DO NOT PROVIDE:
  - fsync ensures data is flushed to disk (crash durability) but does NOT
    make the file append-only or tamper-proof. An attacker with write
    access to the file can modify, truncate, or replace it.
  - Append-only and tamper-proof properties require separate controls:
    OS-level append-only flags (chattr +a on Linux), immutable backups,
    WORM storage, or a transparency log with an external verifier.
  - M1.3 FileAuditSink uses fsync for crash durability. Its threat model
    is "crash recovery", NOT "adversary with disk access". See
    "PERSISTENT STORAGE AND THE M1.3 RECOVERY MODEL" below. An explicit
    threat model document (M0) will define which adversaries are in scope.

  RANGE VERIFICATION (start_seq / end_seq):
  - verify_chain(start_seq=N, end_seq=M) verifies the sub-chain of events
    with sequence numbers in [N, M] (1-based, inclusive).
  - For start_seq <= 1, verification is anchored to GENESIS_HASH (full
    chain verification from the beginning).
  - For start_seq > 1, verification is anchored to events[start_seq-2]
    (the event before the range). The anchor's self-hash is recomputed
    and compared against events[start_seq-1].prev_hash. If the anchor
    itself is tampered, this is detected. Events BEFORE the anchor
    (events[0..start_seq-3]) are NOT verified — they are trusted as
    previously verified or externally anchored.
  - Range verification is useful for incremental verification ("verify
    the last N events without re-walking the whole chain") but does NOT
    provide full-chain authenticity. For full verification, call
    verify_chain() with no arguments.

  MALFORMED EVENTS:
  - verify_chain() catches exceptions during hash computation (TypeError,
    ValueError, AttributeError) and returns False. A malformed event
    (e.g., details=None) does NOT silently pass and does NOT crash the
    verifier.

  PERSISTENT STORAGE AND THE M1.3 RECOVERY MODEL:
  - Storage format: one AuditEvent per line ("JSONL"). Each line holds
    the full event INCLUDING chain metadata (sequence_number,
    prev_hash), serialized with sort_keys=True. Serialization is
    deterministic, so the byte length of the first N events' prefix is
    reproducible without parsing.
  - fsync: every append() writes the line and calls fsync() before
    returning. fsync provides CRASH DURABILITY only. It does NOT make
    the file append-only or tamper-proof: a process (or attacker) with
    write access to the file can still modify, truncate, or replace it.
    Append-only behavior requires separate OS-level controls (e.g.
    chattr +a on Linux), WORM storage, or an external transparency log.
  - FileAuditSink threat model: "the process may crash at any point".
    It is NOT "an adversary with disk access".
  - Recovery model (load_audit_file):
      * strict=True (forensic default): ANY malformed record raises
        AuditStorageError. Nothing is dropped or rewritten.
      * strict=False (crash recovery): a single malformed FINAL record
        is treated as a torn write from a crash and dropped. A
        malformed NON-FINAL record raises AuditStorageError (a torn
        write cannot be followed by later records, so non-final
        corruption indicates damage, not a crash).
      * Blank lines at EOF are ignored; blank or malformed lines
        anywhere else are errors.
  - Resume (FileAuditSink(path, resume=True)):
      * Loads with crash-recovery semantics, physically truncates a
        dropped torn tail so future appends extend a clean prefix,
        verifies the FULL chain from genesis, and refuses to append to
        a chain that fails verification (in that case the file is left
        exactly as found, for forensic inspection).
      * Opening an existing non-empty file without resume=True raises
        FileExistsError, preventing accidental mixing of two chains.
  - Append failure: if write/fsync fails, append() best-effort rolls
    back (truncates the partial line, removes the in-memory copy) and
    re-raises. Callers cannot mistake an unrecorded event for a
    recorded one.
  - Not thread-safe: a single writer per file. Serialize concurrent
    writers externally.

  TRUSTED CHAIN CHECKPOINT (M1.3):
  - write_checkpoint(events, path) records {format, version,
    chain_length, tip_hash, created_at} to a separate file. Because the
    chain is hash-linked from genesis, the tip hash is a commitment to
    the ENTIRE chain: recomputing the chain and comparing the final
    self-hash detects modification of ANY event, truncation, and
    reordering.
  - verify_checkpoint(events, path) recomputes the chain (verifying
    prev_hash links AND sequence numbering) and compares tip hash and
    chain_length against the checkpoint. It returns False on any
    mismatch or malformed checkpoint; it never raises on bad input.
  - TRUST MODEL (READ BEFORE RELYING ON THIS):
      * The checkpoint authenticates the last event (and thereby the
        whole chain) ONLY IF the checkpoint lives in a trust domain the
        attacker cannot write to: an out-of-band copy, another host, an
        externally signed blob, or an equivalent anchor.
      * If an attacker can rewrite BOTH the chain file and the
        checkpoint file, the checkpoint provides NO authenticity: the
        attacker can recompute the chain and write a matching
        checkpoint. This is NOT a digital signature; no key material
        is involved.
      * With an independently trusted checkpoint, final-event
        modification IS detectable — closing the main gap of the bare
        hash chain (see "WHAT THE HASH CHAIN DOES NOT PROVIDE").

  BOTTOM LINE:
  - M1.1/M1.2 provide tamper DETECTION for non-terminal events within a
    single process lifetime. They do NOT provide tamper PREVENTION,
    last-event authentication, or protection against full-chain rewrites.
  - M1.3 adds durable storage (crash-safe, NOT tamper-proof) and a
    checkpoint mechanism that detects last-event tampering and
    truncation WHEN the checkpoint is stored in an independent trust
    domain. Neither feature prevents an attacker who controls the
    storage from rewriting everything.
  - M1.4 SQLiteAuditSink provides the same hash-chain semantics over a
    SQLite single-file database. SQLite's ACID transactions give crash
    durability (comparable to fsync on a flat file) and a DB-level
    PRIMARY KEY constraint that makes duplicate or out-of-order seq
    numbers raise IntegrityError instead of silently corrupting the
    chain. SQLite does NOT make the database tamper-proof: an attacker
    with write access to the .db file can modify or replace rows.
    The hash chain still provides tamper DETECTION (same as the
    FileAuditSink path); the same checkpoint mechanism applies.
  - Production deployments requiring full tamper-evidence must integrate
    an external trust anchor (out of scope for UPMP core, per
    ARCHITECTURE.md's security boundary).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, List, Optional, Protocol, Sequence

from .closed_loop import AuditEvent


GENESIS_HASH = ""


def canonical_json(event: "AuditEvent") -> str:
    """Deterministic JSON serialization of an AuditEvent's payload.

    Excludes sequence_number and prev_hash (chain metadata, not payload).
    Uses sort_keys=True for deterministic field ordering. Uses
    default=str to handle non-JSON-serializable values (e.g. datetime
    objects, custom classes). This is preferable to raising because the
    audit trail must never fail to record an event.

    Raises TypeError if event.details is None or not a mapping (propagated
    to verify_chain which catches it and returns False).
    """
    payload = {
        "event": event.event,
        "step_id": event.step_id,
        "timestamp": event.timestamp,
        "details": dict(event.details),
        "branch_id": event.branch_id,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def event_hash(prev_hash: str, event: "AuditEvent") -> str:
    """SHA-256 of (prev_hash || NUL || canonical_json(event_payload)).

    The NUL byte separator prevents ambiguity when concatenating prev_hash
    and the JSON payload (a prev_hash ending in characters that could
    appear at the start of the JSON would otherwise be ambiguous).

    This is the "self hash" of an event — the value stored in the NEXT
    event's prev_hash field.
    """
    h = hashlib.sha256()
    h.update(prev_hash.encode("utf-8"))
    h.update(b"\x00")
    h.update(canonical_json(event).encode("utf-8"))
    return h.hexdigest()


class AuditSink(Protocol):
    """Append-only audit sink with hash-chain verification.

    Implementations MUST:
      - Assign a monotonically increasing sequence_number (1-based) to each
        event at append() time.
      - Assign prev_hash = event_hash(prev_event.prev_hash, prev_event)
        (or GENESIS_HASH = "" for the first event). This stores the
        preceding event's self-hash in the current event's prev_hash field.
      - Never mutate or delete previously appended events.
      - Return True from verify_chain() iff the chain is internally
        consistent (prev_hash links and sequence_numbers are valid).

    Implementations MAY:
      - Persist events to disk, database, or network (M1.3, M1.4).
      - Support range-limited verification via start_seq / end_seq.

    Security note:
      verify_chain() returning True means the chain is internally
      consistent — it does NOT mean the chain is authentic. An attacker
      who can rewrite the entire chain can produce a valid-looking result.
      See the module docstring for the full security properties and
      limitations.
    """

    def append(self, event: "AuditEvent") -> int:
        """Append an event. Returns the assigned sequence number (1-based)."""
        ...

    def verify_chain(self, start_seq: int = 0, end_seq: Optional[int] = None) -> bool:
        """Verify the hash chain. Returns True if intact, False if tampered.

        Args:
            start_seq: 1-based starting sequence number. 0 means "from the
                beginning" (treated as 1). For start_seq > 1, the chain is
                verified from event start_seq onward, anchored to the event
                before start_seq. Events before the anchor are NOT verified.
            end_seq: 1-based ending sequence number (inclusive). None means
                "to the end".

        Returns:
            True if the verified range is internally consistent (prev_hash
            links and sequence_numbers are valid). False if any event in
            the range is tampered, reordered, deleted, duplicated, or
            malformed.

        Note:
            Full-chain verification (start_seq=0) detects tampering of
            all events EXCEPT the last one. See module docstring.
        """
        ...


class InMemoryAuditSink:
    """Default in-memory audit sink with SHA-256 hash chaining.

    Events are stored in a public list (self.events) so tests can simulate
    tampering via dataclasses.replace. Sequence numbers start at 1.

    Limitation: in-memory storage does NOT survive process exit. Use
    FileAuditSink (M1.3) or SQLiteAuditSink (M1.4) for persistence.
    This implementation is suitable for testing and for single-process
    workflows where the audit trail is consumed before process exit.

    Limitation: a hash chain detects tampering of non-terminal events but
    does NOT authenticate the last event and does NOT protect against
    full-chain rewrites. See the module docstring for the full security
    properties and limitations.
    """

    def __init__(self) -> None:
        self.events: List["AuditEvent"] = []

    def append(self, event: "AuditEvent") -> int:
        seq = len(self.events) + 1
        if self.events:
            last = self.events[-1]
            prev_hash = event_hash(last.prev_hash, last)
        else:
            prev_hash = GENESIS_HASH
        chained = replace(event, sequence_number=seq, prev_hash=prev_hash)
        self.events.append(chained)
        return seq

    def verify_chain(self, start_seq: int = 0, end_seq: Optional[int] = None) -> bool:
        """Verify the hash chain (or a sub-range). See AuditSink.verify_chain."""
        try:
            return self._verify_chain_impl(start_seq, end_seq)
        except (TypeError, ValueError, AttributeError):
            # Any exception during hash computation or field access indicates
            # a malformed event (e.g., details=None, missing fields). Treat
            # as a broken chain — do NOT let malformed events silently pass
            # and do NOT crash the verifier (DoS vector).
            return False

    def _verify_chain_impl(self, start_seq: int, end_seq: Optional[int]) -> bool:
        if not self.events:
            return True

        # Normalize: start_seq is 1-based; 0 means "from the beginning" (= 1).
        start = max(1, start_seq)

        # end_seq is 1-based inclusive; None means "to the end".
        end = len(self.events) if end_seq is None else min(end_seq, len(self.events))

        # Empty range: nothing to verify.
        if start > end:
            return True

        # Compute the expected prev_hash at the start of the range.
        # For start == 1, the anchor is GENESIS_HASH (full-chain verification).
        # For start > 1, the anchor is events[start-2] (the event before the
        # range). The anchor's self-hash is recomputed and used as the expected
        # prev_hash for events[start-1]. This means:
        #   - If the anchor itself (events[start-2]) is tampered, its recomputed
        #     hash won't match events[start-1].prev_hash → detected.
        #   - Events BEFORE the anchor (events[0..start-3]) are NOT verified.
        #     They are trusted as previously verified or externally anchored.
        if start == 1:
            expected_prev_hash = GENESIS_HASH
        else:
            anchor_idx = start - 2  # 0-based index of the anchor event
            if anchor_idx < 0 or anchor_idx >= len(self.events):
                return False  # Anchor out of range — cannot verify
            anchor = self.events[anchor_idx]
            expected_prev_hash = event_hash(anchor.prev_hash, anchor)

        # Walk the range [start-1, end-1] (0-based indices).
        for i in range(start - 1, end):
            event = self.events[i]
            if event.prev_hash != expected_prev_hash:
                return False
            if event.sequence_number != i + 1:
                return False
            expected_prev_hash = event_hash(event.prev_hash, event)

        return True

    def __len__(self) -> int:
        return len(self.events)


class AuditStorageError(Exception):
    """Raised for durable-storage failures: malformed records, refusal to
    resume a corrupted chain, or refusal to checkpoint an invalid chain.

    This error indicates the storage layer could not honor an operation.
    It is NOT evidence of tampering by itself (a torn final write from a
    crash produces the same error as an edit); see the recovery model in
    the module docstring.
    """


class TransparencyLog(Protocol):
    """External transparency log integration seam.

    A transparency log is an append-only, independently-verifiable
    record of chain tips. UPMP does NOT implement one — this Protocol
    is the seam that allows a production deployment to plug in a real
    transparency log (Certificate Transparency style, append-only
    Merkle tree, blockchain, etc.).

    The threat this closes (THREAT.md T10): an attacker with disk
    write access can rewrite the entire chain file AND the checkpoint
    file, producing a valid-looking chain + matching checkpoint. If
    the chain's tip is also published to a transparency log the
    attacker cannot write, the forgery is detectable: recompute the
    chain from the (possibly tampered) file and compare against what
    the transparency log says was published.

    Implementations MUST:
      - publish(tip_hash, chain_length, created_at) -> None: record a
        chain tip. Idempotent — publishing the same tip twice is a
        no-op (or at least does not corrupt the log).
      - get_latest() -> Optional[dict]: return the most recent
        published tip, or None if nothing has been published. The
        dict has keys {tip_hash, chain_length, created_at}.
      - get_history() -> Sequence[dict]: return all published tips in
        publication order.

    Implementations MUST NOT:
      - Accept a modification or deletion of a previously published
        tip. Append-only is the defining property of a transparency
        log.
      - Trust the caller to provide a correct tip_hash. The log
        records what it was given; verification is the caller's job.

    Trust model:
      - The transparency log is assumed to be in an independent trust
        domain: the attacker cannot modify its records. This is the
        same assumption made for trusted checkpoints (see module
        docstring), but extended to a stream of tips over time rather
        than a single snapshot.
      - The transparency log does NOT sign tips (no key material).
        Authenticity comes entirely from where it is stored.
      - A NullTransparencyLog (the default) provides NO protection —
        it accepts everything and verifies nothing. It exists only to
        preserve backward compatibility for callers that do not opt
        in to transparency logging.
    """

    def publish(self, tip_hash: str, chain_length: int,
                created_at: Optional[str] = None) -> None:
        """Append a chain tip to the transparency log. Idempotent."""
        ...

    def get_latest(self) -> Optional[dict]:
        """Return the most recent published tip, or None."""
        ...

    def get_history(self) -> Sequence[dict]:
        """Return all published tips in publication order."""
        ...


class NullTransparencyLog:
    """Default no-op transparency log. Provides NO protection.

    Accepts all publishes and returns None/empty on queries. Use this
    when transparency logging is not required (e.g. testing, single-
    process workflows). Backward-compatible default for sinks that do
    not opt in.
    """

    def publish(self, tip_hash: str, chain_length: int,
                created_at: Optional[str] = None) -> None:
        pass

    def get_latest(self) -> Optional[dict]:
        return None

    def get_history(self) -> Sequence[dict]:
        return ()


class InMemoryTransparencyLog:
    """In-process transparency log for testing and demos.

    Records every published tip in a list. NOT a real transparency
    log — it lives in the same process as the writer, so an attacker
    who controls the process can modify it. Use this only for testing
    the integration seam, not for production protection.
    """

    def __init__(self) -> None:
        self._entries: List[dict] = []

    def publish(self, tip_hash: str, chain_length: int,
                created_at: Optional[str] = None) -> None:
        entry = {
            "tip_hash": tip_hash,
            "chain_length": chain_length,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        # Idempotent: if the latest entry already matches, don't duplicate.
        if self._entries and self._entries[-1] == entry:
            return
        self._entries.append(entry)

    def get_latest(self) -> Optional[dict]:
        return self._entries[-1] if self._entries else None

    def get_history(self) -> Sequence[dict]:
        return tuple(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def verify_against_transparency_log(events: Sequence[AuditEvent],
                                     log: TransparencyLog) -> bool:
    """Verify a chain against the latest tip in a transparency log.

    Recomputes the chain from genesis (verifying prev_hash links and
    sequence numbering) and compares the tip hash and chain length
    against the transparency log's most recent published entry.

    Returns True only on an exact match of tip hash and chain length.
    Returns False if: the chain is internally inconsistent, the
    transparency log is empty, or the tip/length do not match.

    This is the mechanism that closes THREAT.md T10 (full-chain
    rewrite by an attacker with disk write access) — PROVIDED the
    transparency log is in an independent trust domain. An attacker
    who controls both the chain file AND the transparency log
    defeats this; that requires an external trust anchor (signed
    genesis, hardware root of trust) which is out of UPMP core scope.

    Never raises on bad input; returns False instead.
    """
    # Duck-typed: any object with get_latest() that returns a dict (or
    # None) is accepted. We don't use isinstance(log, TransparencyLog)
    # because Protocol without @runtime_checkable doesn't support it,
    # and adding @runtime_checkable would still require the caller to
    # import the Protocol explicitly.
    if not hasattr(log, "get_latest"):
        return False
    latest = log.get_latest()
    if latest is None:
        return False  # nothing published — cannot verify
    tip = _recompute_tip(events)
    if tip is None:
        return False
    length = latest.get("chain_length")
    stored_tip = latest.get("tip_hash")
    if not isinstance(length, int) or not isinstance(stored_tip, str):
        return False
    return length == len(events) and stored_tip == tip


CHECKPOINT_FORMAT = "upmp-audit-checkpoint"
CHECKPOINT_VERSION = 1


def event_to_line(event: AuditEvent) -> str:
    """Serialize a chained AuditEvent to one JSONL line.

    Includes chain metadata (sequence_number, prev_hash) alongside the
    payload fields. Uses sort_keys=True and compact separators for a
    deterministic byte representation (required by the resume-time
    prefix-length computation). Non-JSON-native values are coerced via
    default=str — the same coercion canonical_json uses when hashing, so
    hashes computed at append time remain valid after a reload.

    Raises TypeError if event.details is not a mapping (same rule as
    canonical_json): FileAuditSink.append() rejects such events up front
    rather than persisting a record that would poison the chain.
    """
    record = {
        "event": event.event,
        "step_id": event.step_id,
        "timestamp": event.timestamp,
        "details": dict(event.details),
        "sequence_number": event.sequence_number,
        "prev_hash": event.prev_hash,
        "branch_id": event.branch_id,
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def event_from_line(line: str) -> AuditEvent:
    """Reconstruct a chained AuditEvent from one JSONL line.

    Raises json.JSONDecodeError (a ValueError) for torn/unparseable
    lines and KeyError for structurally incomplete records — callers
    (load_audit_file, resume) treat both as a malformed record.
    Unknown extra keys are ignored (forward compatibility).
    """
    record = json.loads(line)
    return AuditEvent(
        event=record["event"],
        step_id=record["step_id"],
        timestamp=record["timestamp"],
        details=record["details"],
        sequence_number=record["sequence_number"],
        prev_hash=record["prev_hash"],
        # branch_id was added in M7. Old audit files (pre-M7) do not
        # have this field; default to "default" so they load cleanly.
        branch_id=record.get("branch_id", "default"),
    )


def load_audit_file(path: os.PathLike, strict: bool = True) -> List[AuditEvent]:
    """Load a persisted chain from a JSONL audit file.

    Recovery model (see module docstring):
      - strict=True (forensic): any malformed record raises
        AuditStorageError with its line number. The file is never
        modified.
      - strict=False (crash recovery): at most ONE malformed FINAL
        record is dropped (torn write from a crash). A malformed
        NON-FINAL record raises — later records cannot follow a torn
        write, so non-final corruption is damage, not a crash.
      - Blank lines at EOF are ignored.

    Returns the list of chained AuditEvents (possibly empty). This
    function does NOT verify hash links; use verify_audit_file() or
    FileAuditSink(resume=True) for verified access.
    """
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()  # ignore trailing blank lines at EOF

    events: List[AuditEvent] = []
    for idx, line in enumerate(lines):
        malformed = False
        if not line.strip():
            malformed = True
        else:
            try:
                events.append(event_from_line(line))
            except (ValueError, KeyError, TypeError):
                malformed = True
        if malformed:
            if strict or idx != len(lines) - 1:
                raise AuditStorageError(
                    "malformed audit record at line %d of %s%s"
                    % (idx + 1, path,
                       "" if strict else " (non-final; crash recovery not applicable)"))
            # non-strict: drop the single torn final record
    return events


def verify_audit_file(path: os.PathLike) -> bool:
    """Forensic on-disk verification: load strict and verify the full chain.

    Returns True iff the file parses cleanly AND the chain is internally
    consistent. Raises AuditStorageError if the file contains malformed
    records (parse errors are reported, not silently treated as False,
    so a corrupt file cannot be mistaken for an empty-but-valid one).
    """
    events = load_audit_file(path, strict=True)
    verifier = InMemoryAuditSink()
    verifier.events = list(events)
    return verifier.verify_chain()


def _recompute_tip(events: Sequence[AuditEvent]) -> Optional[str]:
    """Recompute the chain tip from genesis, verifying links and sequence
    numbers. Returns None if the chain is broken or malformed."""
    try:
        running = GENESIS_HASH
        for i, event in enumerate(events):
            if event.prev_hash != running:
                return None
            if event.sequence_number != i + 1:
                return None
            running = event_hash(event.prev_hash, event)
        return running
    except (TypeError, ValueError, AttributeError):
        return None


def write_checkpoint(events: Sequence[AuditEvent], path: os.PathLike,
                     created_at: Optional[str] = None) -> dict:
    """Write a trusted chain checkpoint to a separate file.

    The checkpoint commits to the ENTIRE chain via its tip hash plus the
    chain length. Refuses (AuditStorageError) to checkpoint a chain that
    fails internal verification, so a checkpoint can never lend false
    credibility to a broken chain.

    TRUST MODEL: the checkpoint authenticates the chain only if the
    checkpoint file is stored where an attacker cannot rewrite it
    (out-of-band copy, externally signed blob, another host). An
    attacker who controls both files gains nothing new to fear from
    this mechanism. See module docstring.

    Returns the checkpoint record that was written.
    """
    tip = _recompute_tip(events)
    if tip is None:
        raise AuditStorageError(
            "refusing to checkpoint: chain fails internal verification")
    record = {
        "format": CHECKPOINT_FORMAT,
        "version": CHECKPOINT_VERSION,
        "chain_length": len(events),
        "tip_hash": tip,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_directory(os.path.dirname(os.path.abspath(path)) or ".")
    return record


def verify_checkpoint(events: Sequence[AuditEvent], checkpoint_path: os.PathLike) -> bool:
    """Verify a chain against a trusted checkpoint.

    Recomputes the chain from genesis (checking prev_hash links and
    sequence numbering) and compares the tip hash and chain length
    against the checkpoint. Returns True only on an exact match of
    format, version, length, and tip hash.

    This is the mechanism that detects tampering with the LAST event
    (and truncation), which verify_chain() alone cannot see — provided
    the checkpoint itself is trusted (see TRUST MODEL in the module
    docstring). Never raises on bad input; returns False instead.
    """
    try:
        with open(checkpoint_path, "r", encoding="utf-8") as fh:
            record = json.loads(fh.read())
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    if record.get("format") != CHECKPOINT_FORMAT or record.get("version") != CHECKPOINT_VERSION:
        return False
    tip = _recompute_tip(events)
    if tip is None:
        return False
    length = record.get("chain_length")
    stored_tip = record.get("tip_hash")
    if not isinstance(length, int) or not isinstance(stored_tip, str):
        return False
    return length == len(events) and stored_tip == tip


def _fsync_directory(dir_path: str) -> None:
    """Best-effort directory fsync (makes file creation/rename durable).

    No-op on platforms where fsync on a directory fd is unsupported
    (e.g. some Windows configurations). Not a security control.
    """
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class FileAuditSink:
    """Durable, hash-chained audit sink backed by a JSONL file.

    One chained AuditEvent per line; every append() is flushed and
    fsync'd before the sequence number is returned, so an event is
    either durably recorded or append() raises (best-effort rollback of
    the partial write and the in-memory copy).

    Storage model:
      - __init__(path) opens a NEW chain: an existing non-empty file
        raises FileExistsError (pass resume=True to continue it).
      - __init__(path, resume=True) loads the existing chain with
        crash-recovery semantics (a torn final record is dropped and
        physically truncated), verifies the FULL chain, and refuses
        (AuditStorageError, file untouched) to append to a corrupted
        chain. Sequence numbering continues from the loaded tip.
      - verify_chain() verifies the in-memory chain state, which tracks
        the file as written by this sink. For forensic verification of
        a file that may have been modified externally, use
        verify_audit_file(path).

    Threat model and limits (see module docstring for the full text):
      - fsync gives CRASH DURABILITY, not tamper resistance. This sink
        does NOT provide append-only or tamper-proof storage; those
        require OS-level controls (chattr +a), WORM storage, or an
        external transparency log.
      - The hash chain does not authenticate the last event. Pair this
        sink with write_checkpoint()/verify_checkpoint() and store the
        checkpoint in an independent trust domain to detect last-event
        tampering and truncation.
      - Not thread-safe: one writer per file.

    Usage:
        with FileAuditSink("audit.jsonl") as sink:
            sink.append(event)          # returns 1-based sequence number
            assert sink.verify_chain()
        # later, possibly in another process:
        events = load_audit_file("audit.jsonl")
    """

    def __init__(self, path: os.PathLike, resume: bool = False,
                 transparency_log: Optional[TransparencyLog] = None) -> None:
        self.path = os.fspath(path)
        self._mem = InMemoryAuditSink()
        self._handle = None
        self.transparency_log = transparency_log if transparency_log is not None else NullTransparencyLog()
        existed = os.path.exists(self.path)
        if existed and os.path.getsize(self.path) > 0:
            if not resume:
                raise FileExistsError(
                    "audit file already exists and is non-empty: %s; "
                    "pass resume=True to continue the existing chain" % self.path)
            self._resume_from_disk()
        self._handle = open(self.path, "a", encoding="utf-8", newline="\n")
        if not existed:
            _fsync_directory(os.path.dirname(os.path.abspath(self.path)) or ".")
        # If a transparency log is configured and the chain is non-empty
        # after resume, publish the current tip so verification works
        # immediately without waiting for the next append.
        if len(self._mem) > 0:
            self._publish_tip()

    def _publish_tip(self) -> None:
        """Publish the current chain tip to the transparency log (if any).

        Best-effort: a transparency-log failure is logged via exception
        propagation (the caller can decide whether to retry or abort).
        The tip is recomputed from the in-memory chain, not trusted from
        a cached value."""
        if not self._mem.events:
            return
        tip = _recompute_tip(self._mem.events)
        if tip is not None:
            self.transparency_log.publish(tip, len(self._mem.events))

    def _resume_from_disk(self) -> None:
        """Load and verify an existing chain; drop a torn final record.

        Order matters: the full-chain verification happens BEFORE any
        truncation, so a corrupted file is never modified by a resume
        attempt.
        """
        events = load_audit_file(self.path, strict=False)
        self._mem.events = list(events)
        if not self._mem.verify_chain():
            raise AuditStorageError(
                "refusing to resume: audit chain in %s failed verification; "
                "file left unmodified for forensic inspection" % self.path)
        # Deterministic serialization makes the good-prefix byte length
        # reproducible without parsing the file again.
        prefix_len = sum(len(event_to_line(e).encode("utf-8")) + 1 for e in events)
        if os.path.getsize(self.path) != prefix_len:
            with open(self.path, "r+b") as fh:
                fh.truncate(prefix_len)
                fh.flush()
                os.fsync(fh.fileno())

    def append(self, event: AuditEvent) -> int:
        """Append an event, fsync it, and return the sequence number.

        On write/fsync failure: best-effort rollback (truncate the
        partial line so the next append does not follow a torn record;
        remove the in-memory copy) and re-raise. The caller must treat
        the event as NOT recorded.
        """
        seq = self._mem.append(event)
        chained = self._mem.events[-1]
        offset = self._handle.tell()
        try:
            self._handle.write(event_to_line(chained) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except Exception:
            try:
                self._handle.flush()
                os.truncate(self.path, offset)
                self._handle.seek(offset)
            except OSError:
                pass
            self._mem.events.pop()
            raise
        # Publish the new tip to the transparency log (if configured).
        # Done AFTER the fsync succeeds so we never publish a tip for
        # an event that did not durably land.
        self._publish_tip()
        return seq

    def verify_chain(self, start_seq: int = 0, end_seq: Optional[int] = None) -> bool:
        """Verify the in-memory chain state. See AuditSink.verify_chain."""
        return self._mem.verify_chain(start_seq, end_seq)

    @property
    def events(self) -> List[AuditEvent]:
        """Chained events currently held in memory (mirrors the file as
        written by this sink). Treat as read-only."""
        return self._mem.events

    def __len__(self) -> int:
        return len(self._mem)

    def write_checkpoint(self, path: os.PathLike,
                         created_at: Optional[str] = None) -> dict:
        """Write a trusted checkpoint of the current chain (see
        write_checkpoint). Store the checkpoint file in an independent
        trust domain for it to provide last-event authentication."""
        return write_checkpoint(self._mem.events, path, created_at=created_at)

    def verify_against_transparency_log(self) -> bool:
        """Verify the in-memory chain against the transparency log's
        latest published tip. See verify_against_transparency_log()."""
        return verify_against_transparency_log(self._mem.events, self.transparency_log)

    def close(self) -> None:
        """Flush, fsync, and close the underlying file. Idempotent."""
        if self._handle is not None and not self._handle.closed:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()

    def __enter__(self) -> "FileAuditSink":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class SQLiteAuditSink:
    """Durable, hash-chained audit sink backed by a SQLite database.

    Single-file SQLite storage with a strict schema: the events table
    has seq INTEGER PRIMARY KEY (1-based, monotonic by construction)
    and the JSONL line text (which carries the full chained event
    including prev_hash) as the canonical record. The PRIMARY KEY
    constraint makes duplicate or out-of-order sequence numbers raise
    sqlite3.IntegrityError at insert time, so the DB itself refuses
    to accept a chain break at the storage layer. The line column is
    authoritative — there is no separate prev_hash column to fall out
    of sync with the JSON payload.

    Crash durability: journal_mode=WAL + synchronous=NORMAL flushes
    each commit to disk before COMMIT returns. This is comparable to
    FileAuditSink's per-append fsync and gives the same crash-recovery
    guarantee: a committed event is on disk; an uncommitted event is
    not (rolled back by SQLite on next open).

    Resume model:
      - __init__(path) opens a NEW chain: an existing non-empty
        database raises FileExistsError (pass resume=True to continue).
      - __init__(path, resume=True) loads all rows in seq order,
        verifies the FULL hash chain, and refuses (AuditStorageError,
        DB untouched) to append to a corrupted chain. Sequence
        numbering continues from the loaded tip.
      - SQLite's own crash recovery (WAL replay on next open) handles
        torn writes; there is no separate torn-tail truncation step.

    Threat model and limits (see module docstring for the full text):
      - SQLite ACID gives CRASH DURABILITY, not tamper resistance.
        An attacker with write access to the .db file can modify or
        replace rows; the hash chain DETECTS this on the next
        verify_chain() call (or via verify_checkpoint), but does not
        PREVENT it. Append-only or tamper-proof storage requires OS-
        level controls, WORM storage, or an external transparency log.
      - Same last-event authentication gap as FileAuditSink: pair with
        write_checkpoint()/verify_checkpoint() and store the
        checkpoint in an independent trust domain.
      - Not thread-safe: one writer per database file (SQLite's
        default serialized access does NOT substitute for application-
        level coordination of audit writes).

    Usage:
        with SQLiteAuditSink("audit.db") as sink:
            sink.append(event)          # returns 1-based sequence number
            assert sink.verify_chain()
        # later, possibly in another process:
        with SQLiteAuditSink("audit.db", resume=True) as sink:
            assert len(sink) == 1
    """

    def __init__(self, path: os.PathLike, resume: bool = False,
                 transparency_log: Optional[TransparencyLog] = None) -> None:
        import sqlite3  # stdlib, imported lazily so audit.py stays
                         # importable on minimal Python builds
        self.path = os.fspath(path)
        self._mem = InMemoryAuditSink()
        self.transparency_log = transparency_log if transparency_log is not None else NullTransparencyLog()
        existed = os.path.exists(self.path) and os.path.getsize(self.path) > 0
        if existed and not resume:
            raise FileExistsError(
                "audit database already exists and is non-empty: %s; "
                "pass resume=True to continue the existing chain" % self.path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        # WAL + synchronous=NORMAL: each COMMIT is durable on disk
        # before returning. Faster than synchronous=FULL with the same
        # crash-durability guarantee for our append-only workload.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " seq INTEGER PRIMARY KEY,"
            " line TEXT NOT NULL"
            ")"
        )
        if existed:
            self._resume_from_db()
        # Publish the current tip if non-empty after resume.
        if len(self._mem) > 0:
            self._publish_tip_sqlite()

    def _resume_from_db(self) -> None:
        """Load all rows in seq order and verify the full hash chain.

        Refuses (AuditStorageError, DB left untouched) to append to a
        chain that fails verification. SQLite's own WAL replay handles
        any in-flight transaction from a crash; there is no separate
        torn-tail truncation step here.
        """
        rows = self._conn.execute(
            "SELECT line FROM events ORDER BY seq"
        ).fetchall()
        events = []
        for (line,) in rows:
            try:
                events.append(event_from_line(line))
            except (ValueError, KeyError, TypeError) as exc:
                raise AuditStorageError(
                    "malformed audit record in %s: %s" % (self.path, exc))
        self._mem.events = events
        if not self._mem.verify_chain():
            raise AuditStorageError(
                "refusing to resume: audit chain in %s failed verification; "
                "database left unmodified for forensic inspection" % self.path)

    def append(self, event: AuditEvent) -> int:
        """Append an event, COMMIT it (durable), and return the seq.

        On any storage failure (e.g., IntegrityError from a duplicate
        seq, TypeError from a malformed event payload, or disk-full):
        rolls back the in-memory copy and re-raises. The caller must
        treat the event as NOT recorded.
        """
        seq = self._mem.append(event)
        chained = self._mem.events[-1]
        try:
            line = event_to_line(chained)  # may raise TypeError on bad details
            with self._conn:
                self._conn.execute(
                    "INSERT INTO events (seq, line) VALUES (?, ?)",
                    (chained.sequence_number, line),
                )
        except Exception:
            self._mem.events.pop()
            raise
        # Publish the new tip after the COMMIT succeeds.
        tip = _recompute_tip(self._mem.events)
        if tip is not None:
            self.transparency_log.publish(tip, len(self._mem.events))
        return seq

    def _publish_tip_sqlite(self) -> None:
        """Publish the current chain tip to the transparency log."""
        if not self._mem.events:
            return
        tip = _recompute_tip(self._mem.events)
        if tip is not None:
            self.transparency_log.publish(tip, len(self._mem.events))

    def verify_chain(self, start_seq: int = 0, end_seq: Optional[int] = None) -> bool:
        """Verify the in-memory chain state. See AuditSink.verify_chain."""
        return self._mem.verify_chain(start_seq, end_seq)

    @property
    def events(self) -> List[AuditEvent]:
        """Chained events currently held in memory (mirrors the DB as
        written by this sink). Treat as read-only."""
        return self._mem.events

    def __len__(self) -> int:
        return len(self._mem)

    def write_checkpoint(self, path: os.PathLike,
                         created_at: Optional[str] = None) -> dict:
        """Write a trusted checkpoint of the current chain (see
        write_checkpoint). Store the checkpoint file in an independent
        trust domain for it to provide last-event authentication."""
        return write_checkpoint(self._mem.events, path, created_at=created_at)

    def verify_against_transparency_log(self) -> bool:
        """Verify the in-memory chain against the transparency log's
        latest published tip. See verify_against_transparency_log()."""
        return verify_against_transparency_log(self._mem.events, self.transparency_log)

    def close(self) -> None:
        """Close the underlying database connection. Idempotent."""
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass  # checkpoint is best-effort; close() must not raise
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SQLiteAuditSink":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
