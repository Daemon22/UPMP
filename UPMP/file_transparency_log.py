"""File-backed transparency log with Merkle root commitment.

External (production) scope — provides a file-backed TransparencyLog
implementation that proves append-only behavior via a Merkle root.
Each published tip is a leaf in a Merkle tree; the root commits to
the entire log history. Retroactive modification, insertion, or
truncation of any entry breaks the root.

This closes the External milestone's "production transparency log"
item. The InMemoryTransparencyLog (from UPMP/audit.py) is suitable
for testing but lives in the same process as the writer — an attacker
who controls the process can modify it. FileTransparencyLog writes
to disk with fsync; if the log file and the root file are stored in
a trust domain the attacker cannot write (e.g., a separate append-
only storage system, or a host the attacker cannot compromise), the
log's append-only property is verifiable.

THREAT.md T10 closure (production):
  - The External milestone's InMemoryTransparencyLog proved the seam
    works but is in-process. FileTransparencyLog is the file-backed
    production implementation: it writes JSONL entries with fsync,
    maintains a Merkle root over all entries, and verifies the root
    on read.
  - Combined with an independent trust anchor for the root file
    (e.g., publishing the root to a public ledger, or signing it
    with an Ed25519 key per M4.1), this closes T10 at the production
    level: an attacker who controls the chain file but NOT the
    transparency log file AND the root commitment is detectable.

Design:
  - FileTransparencyLog(path) opens or creates a JSONL log file.
  - publish(tip_hash, chain_length, created_at) appends a line and
    fsyncs, then recomputes the Merkle root and writes it to
    path + ".root" (also fsynced).
  - get_latest() reads the last line of the log file.
  - get_history() reads all lines.
  - verify_integrity() recomputes the Merkle root from the log file
    and compares it to the stored root. Returns True iff they match.
  - merkle_root(entries) is a module-level function that computes
    the root from a list of entries (for external verification).

Merkle tree construction:
  - Leaves: SHA-256 of each entry's canonical JSON.
  - Internal nodes: SHA-256(left_child_hash || right_child_hash).
    If a level has an odd number of nodes, the last node is
    duplicated (standard Merkle tree padding).
  - The root is the single hash at the top of the tree.
  - An empty log has root = SHA-256("") (a well-known constant).

Threat model notes:
  - fsync gives crash durability, not tamper resistance. An attacker
    with write access to the log file can modify entries — but if
    they also have to update the root file to match, and the root
    file is in an independent trust domain, the modification is
    detectable.
  - The root file SHOULD be stored separately from the log file
    (different host, signed externally, published to a public ledger)
    for the trust model to hold. If both files are on the same
    writable disk, an attacker controlling the disk can modify both.
  - This is NOT a full Certificate Transparency implementation. A
    production CT log needs consistency proofs (prove that log V2
    is an extension of log V1), inclusion proofs (prove a specific
    entry is in the log), and a gossip protocol. This implementation
    provides the Merkle root commitment; the proofs are deferred.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import List, Optional, Sequence


# The Merkle root of an empty log. This is a well-known constant
# (SHA-256 of the empty string) so that verifiers can recognize an
# empty log without ambiguity.
EMPTY_MERKLE_ROOT = hashlib.sha256(b"").hexdigest()


def _leaf_hash(entry: dict) -> str:
    """SHA-256 of the entry's canonical JSON (sort_keys=True, compact)."""
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def merkle_root(entries: Sequence[dict]) -> str:
    """Compute the Merkle root over a list of entries.

    - Empty list -> EMPTY_MERKLE_ROOT (SHA-256 of empty string).
    - Each leaf is SHA-256 of the entry's canonical JSON.
    - Internal nodes are SHA-256(left || right).
    - Odd levels duplicate the last node (standard padding).
    """
    if not entries:
        return EMPTY_MERKLE_ROOT
    level: List[str] = [_leaf_hash(e) for e in entries]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])  # duplicate last
        next_level: List[str] = []
        for i in range(0, len(level), 2):
            combined = hashlib.sha256(
                (level[i] + level[i + 1]).encode("utf-8")
            ).hexdigest()
            next_level.append(combined)
        level = next_level
    return level[0]


def _entry_to_line(entry: dict) -> str:
    """Serialize an entry to one JSONL line (deterministic)."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)


def _entry_from_line(line: str) -> dict:
    """Parse a JSONL line back to an entry dict."""
    return json.loads(line)


class FileTransparencyLog:
    """File-backed transparency log with Merkle root commitment.

    Stores published tips as JSONL (one per line) in a file. After
    each publish, recomputes the Merkle root over all entries and
    writes it to path + ".root". Both files are fsynced.

    The root file is the trust anchor: if it is stored in an
    independent trust domain (separate host, signed externally,
    published to a public ledger), retroactive modification of the
    log file is detectable via verify_integrity().

    Usage:
        log = FileTransparencyLog("/var/log/upmp/transparency.jsonl")
        log.publish(tip_hash, chain_length)
        assert log.verify_integrity()  # root matches

    On open, an existing log is loaded and its root is verified
    automatically. If verification fails (the log was tampered
    externally), the constructor raises TransparencyLogError.
    """

    def __init__(self, path: os.PathLike,
                 root_path: Optional[os.PathLike] = None) -> None:
        self.path = os.fspath(path)
        self.root_path = os.fspath(root_path) if root_path else self.path + ".root"
        self._entries: List[dict] = []
        self._load_and_verify()

    def _load_and_verify(self) -> None:
        """Load existing entries and verify the Merkle root.

        If the root file doesn't exist (new log), create it.
        If the root file exists but doesn't match the recomputed root,
        raise TransparencyLogError (the log was tampered).
        """
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._entries.append(_entry_from_line(line))
                    except (ValueError, KeyError):
                        raise TransparencyLogError(
                            "malformed entry in %s: %s" % (self.path, line[:80]))
        # Verify or create the root.
        computed = merkle_root(self._entries)
        if os.path.exists(self.root_path):
            stored = self._read_root()
            if stored != computed:
                raise TransparencyLogError(
                    "Merkle root mismatch in %s: stored=%s computed=%s "
                    "(log may have been tampered)" % (self.path, stored[:16], computed[:16]))
        else:
            # New log — write the initial root.
            self._write_root(computed)

    def _read_root(self) -> str:
        """Read the stored Merkle root."""
        with open(self.root_path, "r", encoding="utf-8") as fh:
            return fh.read().strip()

    def _write_root(self, root: str) -> None:
        """Write the Merkle root to the root file with fsync."""
        with open(self.root_path, "w", encoding="utf-8") as fh:
            fh.write(root + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def publish(self, tip_hash: str, chain_length: int,
                created_at: Optional[str] = None) -> None:
        """Append a tip to the log and update the Merkle root.

        The entry is fsynced before the method returns, so a published
        tip is durable. The root file is also fsynced.
        """
        entry = {
            "seq": len(self._entries) + 1,
            "tip_hash": tip_hash,
            "chain_length": chain_length,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        line = _entry_to_line(entry) + "\n"
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._entries.append(entry)
        # Recompute and write the new root.
        new_root = merkle_root(self._entries)
        self._write_root(new_root)

    def get_latest(self) -> Optional[dict]:
        """Return the most recent published entry, or None if empty."""
        return self._entries[-1] if self._entries else None

    def get_history(self) -> Sequence[dict]:
        """Return all entries in publication order."""
        return tuple(self._entries)

    def verify_integrity(self) -> bool:
        """Recompute the Merkle root from the in-memory entries and
        compare it to the stored root. Returns True iff they match.

        This verifies the in-memory state is consistent with the
        stored root. For full verification (the file wasn't tampered
        on disk), reload the log: FileTransparencyLog(path) re-reads
        the file and checks the root at construction time.
        """
        computed = merkle_root(self._entries)
        try:
            stored = self._read_root()
        except OSError:
            return False
        return computed == stored

    def verify_against_external_root(self, external_root: str) -> bool:
        """Verify the log against an externally-provided root (e.g.,
        a root published to a public ledger, or signed with Ed25519
        per M4.1). Returns True iff the recomputed root matches."""
        computed = merkle_root(self._entries)
        return computed == external_root

    @property
    def current_root(self) -> str:
        """The current Merkle root (recomputed from entries)."""
        return merkle_root(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def close(self) -> None:
        """No-op (the log is closed after each publish via the `with`
        block in publish()). Provided for Protocol compatibility."""
        pass

    def __enter__(self) -> "FileTransparencyLog":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class TransparencyLogError(Exception):
    """Raised when a transparency log fails integrity verification.

    This indicates the log file was modified after its root was
    written — either by an attacker, by a storage corruption, or
    by a buggy writer that didn't update the root. Treat as
    forensic evidence, not as a routine error.
    """
    pass
