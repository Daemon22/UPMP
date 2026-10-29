"""External milestone tests — transparency log integration.

Tests that the transparency log seam closes THREAT.md T10 (full-chain
rewrite by an attacker with disk write access) when the log is in an
independent trust domain.

Covers:
  - InMemoryTransparencyLog publish/get_latest/get_history/clear.
  - NullTransparencyLog is a no-op (default, backward compat).
  - verify_against_transparency_log matches recomputed tip.
  - FileAuditSink publishes tips on each append + after resume.
  - SQLiteAuditSink publishes tips on each append + after resume.
  - T10 attack scenario: attacker rewrites chain file but NOT the
    transparency log -> verify_against_transparency_log returns False.
  - T10 with both files compromised: documented limitation (verify
    passes because attacker wrote a matching log).
  - Custom duck-typed transparency log implementation works.
"""
import json
import os
import sqlite3
from dataclasses import replace

import pytest

from UPMP.audit import (
    FileAuditSink,
    InMemoryAuditSink,
    InMemoryTransparencyLog,
    NullTransparencyLog,
    SQLiteAuditSink,
    event_to_line,
    load_audit_file,
    verify_against_transparency_log,
    verify_audit_file,
)
from UPMP.closed_loop import AuditEvent


def make_event(n, details=None):
    return AuditEvent(
        "evt_%d" % n, "step-%d" % n,
        "2026-01-01T00:00:00.%06d+00:00" % n,
        details if details is not None else {"n": n, "payload": "value-%d" % n},
    )


def append_events(sink, count):
    for i in range(1, count + 1):
        sink.append(make_event(i))


class TestInMemoryTransparencyLog:
    def test_empty_log_returns_none(self):
        log = InMemoryTransparencyLog()
        assert log.get_latest() is None
        assert log.get_history() == ()
        assert len(log) == 0

    def test_publish_then_get_latest(self):
        log = InMemoryTransparencyLog()
        log.publish("abc123", 5)
        latest = log.get_latest()
        assert latest["tip_hash"] == "abc123"
        assert latest["chain_length"] == 5
        assert "created_at" in latest

    def test_publish_multiple_keeps_history(self):
        log = InMemoryTransparencyLog()
        log.publish("h1", 1)
        log.publish("h2", 2)
        log.publish("h3", 3)
        history = log.get_history()
        assert len(history) == 3
        assert [h["tip_hash"] for h in history] == ["h1", "h2", "h3"]
        assert log.get_latest()["tip_hash"] == "h3"

    def test_publish_same_tip_idempotent(self):
        log = InMemoryTransparencyLog()
        log.publish("h1", 1, created_at="2026-01-01T00:00:00+00:00")
        log.publish("h1", 1, created_at="2026-01-01T00:00:00+00:00")
        assert len(log) == 1  # not duplicated

    def test_clear(self):
        log = InMemoryTransparencyLog()
        log.publish("h1", 1)
        log.clear()
        assert len(log) == 0
        assert log.get_latest() is None


class TestNullTransparencyLog:
    def test_publish_is_noop(self):
        log = NullTransparencyLog()
        log.publish("h1", 1)  # does not raise
        assert log.get_latest() is None
        assert log.get_history() == ()

    def test_default_sink_has_null_log(self, tmp_path):
        # Without explicit transparency_log, sinks use NullTransparencyLog.
        with FileAuditSink(tmp_path / "a.jsonl") as sink:
            sink.append(make_event(1))
            # NullLog -> get_latest() is None -> verify returns False.
            assert sink.verify_against_transparency_log() is False


class TestVerifyAgainstTransparencyLog:
    def test_matching_tip_returns_true(self):
        from UPMP.audit import InMemoryAuditSink
        sink = InMemoryAuditSink()
        for i in range(1, 4):
            sink.append(make_event(i))
        log = InMemoryTransparencyLog()
        # Publish the correct tip.
        from UPMP.audit import _recompute_tip
        tip = _recompute_tip(sink.events)
        log.publish(tip, len(sink.events))
        assert verify_against_transparency_log(sink.events, log) is True

    def test_mismatched_tip_returns_false(self):
        from UPMP.audit import InMemoryAuditSink
        sink = InMemoryAuditSink()
        for i in range(1, 4):
            sink.append(make_event(i))
        log = InMemoryTransparencyLog()
        log.publish("wrong_hash", 3)  # wrong tip
        assert verify_against_transparency_log(sink.events, log) is False

    def test_mismatched_length_returns_false(self):
        from UPMP.audit import InMemoryAuditSink
        sink = InMemoryAuditSink()
        for i in range(1, 4):
            sink.append(make_event(i))
        log = InMemoryTransparencyLog()
        from UPMP.audit import _recompute_tip
        tip = _recompute_tip(sink.events)
        log.publish(tip, 99)  # wrong length
        assert verify_against_transparency_log(sink.events, log) is False

    def test_empty_log_returns_false(self):
        from UPMP.audit import InMemoryAuditSink
        sink = InMemoryAuditSink()
        sink.append(make_event(1))
        log = InMemoryTransparencyLog()  # nothing published
        assert verify_against_transparency_log(sink.events, log) is False

    def test_broken_chain_returns_false(self):
        from UPMP.audit import InMemoryAuditSink
        sink = InMemoryAuditSink()
        for i in range(1, 4):
            sink.append(make_event(i))
        # Break the chain.
        sink.events[1] = replace(sink.events[1], prev_hash="forged")
        log = InMemoryTransparencyLog()
        log.publish("any", 3)
        assert verify_against_transparency_log(sink.events, log) is False

    def test_non_log_object_returns_false(self):
        from UPMP.audit import InMemoryAuditSink
        sink = InMemoryAuditSink()
        sink.append(make_event(1))
        assert verify_against_transparency_log(sink.events, "not a log") is False
        assert verify_against_transparency_log(sink.events, 42) is False


class TestFileAuditSinkIntegration:
    def test_publishes_tip_on_each_append(self, tmp_path):
        tlog = InMemoryTransparencyLog()
        with FileAuditSink(tmp_path / "a.jsonl", transparency_log=tlog) as sink:
            append_events(sink, 3)
            assert len(tlog) == 3  # one tip per append
            assert tlog.get_latest()["chain_length"] == 3
            assert sink.verify_against_transparency_log() is True

    def test_publishes_tip_after_resume(self, tmp_path):
        tlog = InMemoryTransparencyLog()
        path = tmp_path / "a.jsonl"
        with FileAuditSink(path, transparency_log=tlog) as sink:
            append_events(sink, 2)
        assert len(tlog) == 2
        # Resume with a fresh transparency log (simulating a new
        # process that loads the same chain).
        tlog2 = InMemoryTransparencyLog()
        with FileAuditSink(path, resume=True, transparency_log=tlog2) as sink:
            # Resume publishes the current tip (2 events) immediately.
            assert len(tlog2) == 1
            assert tlog2.get_latest()["chain_length"] == 2
            assert sink.verify_against_transparency_log() is True
            # Append one more -> tip updated to 3 events.
            sink.append(make_event(3))
            assert len(tlog2) == 2
            assert tlog2.get_latest()["chain_length"] == 3

    def test_t10_attack_detected_when_log_independent(self, tmp_path):
        """THE External milestone: an attacker rewrites the chain file
        but CANNOT rewrite the transparency log. verify_against_transparency_log
        catches the forgery."""
        tlog = InMemoryTransparencyLog()  # lives in memory — independent of the file
        path = tmp_path / "a.jsonl"
        with FileAuditSink(path, transparency_log=tlog) as sink:
            append_events(sink, 3)
        # Attacker rewrites the entire chain file with a forged chain
        # (recomputing all prev_hash values consistently).
        attacker_sink = InMemoryAuditSink()
        for i in range(1, 4):
            attacker_sink.append(make_event(i, {"n": i, "payload": "FORGED"}))
        forged_lines = [event_to_line(e) for e in attacker_sink.events]
        with open(path, "w") as fh:
            fh.write("\n".join(forged_lines) + "\n")
        # Load the forged chain and verify against the (un-tampered) transparency log.
        forged_events = load_audit_file(path)
        # The forged chain is internally consistent (verify_chain passes).
        verifier = InMemoryAuditSink()
        verifier.events = list(forged_events)
        assert verifier.verify_chain() is True
        # But the transparency log still has the ORIGINAL tip -> mismatch.
        assert verify_against_transparency_log(forged_events, tlog) is False

    def test_t10_both_compromised_limitation(self, tmp_path):
        """LIMITATION: if the attacker controls BOTH the chain file AND
        the transparency log, verify passes. Documented, not a bug."""
        tlog = InMemoryTransparencyLog()
        path = tmp_path / "a.jsonl"
        with FileAuditSink(path, transparency_log=tlog) as sink:
            append_events(sink, 3)
        # Attacker rewrites both.
        attacker_sink = InMemoryAuditSink()
        for i in range(1, 4):
            attacker_sink.append(make_event(i, {"n": i, "payload": "FORGED"}))
        forged_lines = [event_to_line(e) for e in attacker_sink.events]
        with open(path, "w") as fh:
            fh.write("\n".join(forged_lines) + "\n")
        tlog.clear()  # attacker rewrites the log too
        from UPMP.audit import _recompute_tip
        tlog.publish(_recompute_tip(attacker_sink.events), 3)
        forged_events = load_audit_file(path)
        assert verify_against_transparency_log(forged_events, tlog) is True  # defeated


class TestSQLiteAuditSinkIntegration:
    def test_publishes_tip_on_each_append(self, tmp_path):
        tlog = InMemoryTransparencyLog()
        with SQLiteAuditSink(tmp_path / "a.db", transparency_log=tlog) as sink:
            append_events(sink, 3)
            assert len(tlog) == 3
            assert sink.verify_against_transparency_log() is True

    def test_t10_attack_detected_sqlite(self, tmp_path):
        """Same T10 attack scenario against the SQLite backend."""
        from UPMP.audit import AuditStorageError, event_from_line
        tlog = InMemoryTransparencyLog()
        path = tmp_path / "a.db"
        with SQLiteAuditSink(path, transparency_log=tlog) as sink:
            append_events(sink, 3)
        # Attacker modifies a row's payload via direct SQL.
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT line FROM events WHERE seq = 2").fetchone()
        record = json.loads(row[0])
        record["details"] = {"n": 2, "payload": "FORGED"}
        new_line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        conn.execute("UPDATE events SET line = ? WHERE seq = 2", (new_line,))
        conn.commit()
        conn.close()
        # Resume correctly refuses the tampered chain (M1.4 verify-on-resume).
        with pytest.raises(AuditStorageError, match="failed verification"):
            SQLiteAuditSink(path, resume=True, transparency_log=tlog)
        # Direct: load raw events (bypassing resume verification) and
        # check against the transparency log. The transparency log has
        # the ORIGINAL tip -> the tampered chain's recomputed tip differs.
        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT line FROM events ORDER BY seq").fetchall()
        conn.close()
        events = [event_from_line(r[0]) for r in rows]
        # verify_chain catches the tampered middle event (broken prev_hash link).
        verifier = InMemoryAuditSink()
        verifier.events = list(events)
        assert verifier.verify_chain() is False
        # verify_against_transparency_log also catches it (tip changed).
        assert verify_against_transparency_log(events, tlog) is False


class TestCustomTransparencyLog:
    def test_duck_typed_log_works(self, tmp_path):
        """A custom class implementing publish/get_latest/get_history
        (duck-typed) works with the verify function."""

        class CustomLog:
            def __init__(self):
                self.tips = []
            def publish(self, tip_hash, chain_length, created_at=None):
                self.tips.append({"tip_hash": tip_hash,
                                  "chain_length": chain_length,
                                  "created_at": created_at or "now"})
            def get_latest(self):
                return self.tips[-1] if self.tips else None
            def get_history(self):
                return tuple(self.tips)

        log = CustomLog()
        with FileAuditSink(tmp_path / "a.jsonl", transparency_log=log) as sink:
            append_events(sink, 3)
            assert sink.verify_against_transparency_log() is True
            assert len(log.tips) == 3
