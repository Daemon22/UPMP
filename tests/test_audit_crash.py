"""M1.7 — end-to-end crash recovery test.

Spawns a real child process that uses FileAuditSink, appends 3 fsync'd
events, writes a partial 4th line directly to the file descriptor
(bypassing the sink's append()), and immediately calls os._exit(2) —
no flush, no atexit handlers, no finally blocks. This is the closest
a unit test can get to a true SIGKILL of the audit writer.

The parent test then verifies the recovery model end-to-end:
  1. The file on disk contains 3 clean records + 1 torn final line.
  2. strict=True (forensic) raises AuditStorageError on the torn line.
  3. strict=False (crash recovery) drops the torn tail and returns
     exactly the 3 clean events.
  4. FileAuditSink(resume=True) verifies the chain, physically
     truncates the torn tail, and accepts new appends at seq 4.
  5. After resume + new append, the file is forensically clean
     (verify_audit_file == True) and the chain contains 4 events
     with sequence numbers 1, 2, 3, 4.
  6. The recovered chain's hashes are identical to what the child
     wrote before the crash (proving the recovery path does not
     perturb clean data).
"""
import json
import os
import subprocess
import sys

import pytest

from UPMP.audit import (
    AuditStorageError,
    FileAuditSink,
    event_to_line,
    load_audit_file,
    verify_audit_file,
    write_checkpoint,
    verify_checkpoint,
)
from UPMP.closed_loop import AuditEvent

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRASH_SCRIPT = os.path.join(REPO_ROOT, "scripts", "crash_audit_writer.py")


def _expected_line(i):
    """Reconstruct the exact line the child writes for event i, so the
    parent can verify byte-for-byte that the recovered chain matches."""
    event = AuditEvent(
        "evt_%d" % i,
        "step-%d" % i,
        "2026-01-01T00:00:00.%06d+00:00" % i,
        {"n": i, "payload": "value-%d" % i},
    )
    # The child doesn't pre-chain the event; the sink does. Reproduce
    # the chained form by computing what the sink would have written.
    sink = FileAuditSink.__new__(FileAuditSink)  # bare, no I/O
    from UPMP.audit import InMemoryAuditSink
    sink._mem = InMemoryAuditSink()
    sink.append(event)
    return event_to_line(sink._mem.events[-1])


class TestCrashRecovery:
    """End-to-end crash recovery — child process killed mid-write."""

    def test_child_crashes_mid_write_and_parent_recovers(self, tmp_path):
        path = tmp_path / "crash.jsonl"
        # Spawn child: it will append 3 events, write a partial 4th
        # line, then os._exit(2).
        result = subprocess.run(
            [sys.executable, CRASH_SCRIPT, str(path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 2, "child should exit via os._exit(2); got %d" % result.returncode
        # Sanity: stderr may be empty (clean os._exit) — we don't assert
        # on stderr content because os._exit bypasses Python's stderr
        # flushing too.

        # 1. File exists and contains 3 clean records + 1 torn line.
        assert path.exists()
        raw = path.read_bytes()
        # The torn tail is the partial 4th line; verify it is present
        # (proves the child reached the torn-write step before crashing).
        assert b'evt_4' in raw, "expected torn partial line containing evt_4 not found"
        # The clean prefix is the concatenation of lines for events 1-3.
        # We don't hard-code the byte length (chain hashes are random);
        # instead we verify it by reconstruction below.

        # 2. strict=True (forensic) raises on the torn line.
        with pytest.raises(AuditStorageError, match="line 4"):
            load_audit_file(path, strict=True)

        # 3. strict=False (crash recovery) drops the torn tail.
        events = load_audit_file(path, strict=False)
        assert len(events) == 3
        assert [e.event for e in events] == ["evt_1", "evt_2", "evt_3"]
        assert [e.sequence_number for e in events] == [1, 2, 3]

        # 4. The recovered events' prev_hash chain matches what a fresh
        #    in-memory sink would produce — proving recovery did not
        #    perturb the clean prefix.
        from UPMP.audit import InMemoryAuditSink
        reference = InMemoryAuditSink()
        for i in range(1, 4):
            reference.append(AuditEvent(
                "evt_%d" % i, "step-%d" % i,
                "2026-01-01T00:00:00.%06d+00:00" % i,
                {"n": i, "payload": "value-%d" % i},
            ))
        for recovered, expected in zip(events, reference.events):
            assert recovered.prev_hash == expected.prev_hash
            assert recovered.sequence_number == expected.sequence_number

        # 5. FileAuditSink(resume=True) verifies + truncates + accepts new appends.
        size_before_resume = path.stat().st_size
        with FileAuditSink(path, resume=True) as sink:
            assert len(sink) == 3
            assert sink.verify_chain() is True
            # New append lands at seq 4 (continues the chain).
            seq = sink.append(AuditEvent(
                "evt_4", "step-4",
                "2026-01-01T00:00:00+00:00",
                {"n": 4, "payload": "post-recovery"},
            ))
            assert seq == 4
            assert sink.verify_chain() is True
        size_after_resume = path.stat().st_size

        # 6. The torn tail has been physically truncated: the new file
        #    is exactly the 3-event prefix + the 1 new event line.
        #    (Before truncation it was 3-event prefix + torn partial line.)
        #    We verify by reconstructing the expected size from the
        #    serialized events.
        recovered_events = load_audit_file(path)
        assert len(recovered_events) == 4
        expected_size = sum(len(event_to_line(e).encode("utf-8")) + 1
                            for e in recovered_events)
        assert size_after_resume == expected_size
        # And the torn partial is gone.
        assert b'evt_4", "step_id": "step-4", "timestamp": "2026-01-01T' not in path.read_bytes()

        # 7. Forensic verification passes on the recovered + extended file.
        assert verify_audit_file(path) is True

        # 8. Checkpoint round-trips on the recovered chain.
        checkpoint = tmp_path / "cp.json"
        write_checkpoint(load_audit_file(path), checkpoint)
        assert verify_checkpoint(load_audit_file(path), checkpoint) is True

    def test_clean_child_no_crash_produces_valid_file(self, tmp_path):
        """Control: a child that closes the sink cleanly produces a file
        that passes strict verification (no torn tail to drop)."""
        path = tmp_path / "clean.jsonl"
        # Use a sink in-process for the control case.
        with FileAuditSink(path) as sink:
            for i in range(1, 4):
                sink.append(AuditEvent(
                    "evt_%d" % i, "step-%d" % i,
                    "2026-01-01T00:00:00.%06d+00:00" % i,
                    {"n": i, "payload": "value-%d" % i},
                ))
        events_strict = load_audit_file(path, strict=True)
        assert len(events_strict) == 3
        assert verify_audit_file(path) is True
