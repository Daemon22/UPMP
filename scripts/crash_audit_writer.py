"""M1.7 crash simulator — invoked by tests/test_audit_crash.py as a subprocess.

Behavior:
  1. Opens a FileAuditSink at the path given by argv[1].
  2. Appends 3 clean events (each fsync'd by the sink).
  3. Writes a partial 4th line DIRECTLY to the file descriptor
     (bypassing the sink's append() — no fsync, no flush) to simulate
     a torn write partway through.
  4. Calls os._exit(2) immediately — no atexit handlers, no finally
     blocks, no Python-level cleanup. This is the closest a unit test
     can get to a SIGKILL of the process.

The parent test then opens the file with resume=True and asserts:
  - Exactly 3 events recoverable (the 4th torn write is dropped).
  - The torn tail is physically truncated so the file size matches
    the 3-event prefix length.
  - The chain verifies as intact.
  - New appends land cleanly on top of the recovered chain (seq 4, 5, ...).
"""
import os
import sys

# Make the package importable when run as a subprocess.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from UPMP.audit import FileAuditSink  # noqa: E402
from UPMP.closed_loop import AuditEvent  # noqa: E402

path = sys.argv[1]

sink = FileAuditSink(path)
for i in range(1, 4):
    sink.append(AuditEvent(
        "evt_%d" % i,
        "step-%d" % i,
        "2026-01-01T00:00:00.%06d+00:00" % i,
        {"n": i, "payload": "value-%d" % i},
    ))

# Force the buffered fsync'd tail to be on disk so the clean prefix
# is durable BEFORE we simulate the torn write.
sink._handle.flush()
os.fsync(sink._handle.fileno())

# Simulate a torn write: write partial bytes that look like the start
# of a 4th event's JSON line, then crash WITHOUT flushing or fsyncing.
# We write directly to the underlying fd so the partial bytes land in
# the file (Python's buffer would discard them on os._exit, which
# would not test recovery from a real torn write on disk).
partial_line = b'{"event": "evt_4", "step_id": "step-4", "timestamp": "2026-01-01T'
fd = sink._handle.fileno()
os.write(fd, partial_line)  # bypass Python's BufferedWriter

# CRASH. No flush, no fsync, no close, no atexit. The parent test
# will see: 3 clean records + 1 partial line at EOF.
os._exit(2)
