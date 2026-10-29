"""M1.4 tests — SQLiteAuditSink durability, resume, and DB-level integrity.

Mirrors the FileAuditSink test surface (test_audit_storage.py) adapted
to SQLite: roundtrip, resume continuation, fresh-refuses-existing,
hash-corruption refusal (DB untouched), PRIMARY KEY integrity
enforcement, append-failure rollback, checkpoint interop, and on-disk
tamper detection via verify_chain + checkpoint.

Security-limitation note (also documented in the SQLiteAuditSink
docstring): SQLite ACID gives crash durability, NOT tamper resistance.
The PRIMARY KEY constraint prevents duplicate/out-of-order seq numbers
at the DB layer; the hash chain DETECTS row modification or deletion
on the next verify_chain() call. These are DETECTION mechanisms, not
PREVENTION — an attacker with write access to the .db file can still
modify rows; they cannot do so without breaking the hash chain (which
verify_chain or verify_checkpoint will catch).
"""
import json
import os
import sqlite3
from dataclasses import replace

import pytest

from UPMP.audit import (
    AuditStorageError,
    SQLiteAuditSink,
    event_to_line,
    write_checkpoint,
    verify_checkpoint,
)
from UPMP.closed_loop import AuditEvent


def make_event(n, details=None):
    return AuditEvent(
        "evt_%d" % n,
        "step-%d" % n,
        "2026-01-01T00:00:00.%06d+00:00" % n,
        details if details is not None else {"n": n, "payload": "value-%d" % n},
    )


def append_events(sink, count):
    return [sink.append(make_event(i)) for i in range(1, count + 1)]


def _db_seq_column(path):
    """Read the seq column directly from the SQLite file (forensics)."""
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT seq FROM events ORDER BY seq").fetchall()
    conn.close()
    return [r[0] for r in rows]


class TestSQLiteBasics:
    def test_append_persists_and_roundtrips(self, tmp_path):
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path) as sink:
            seqs = append_events(sink, 3)
            assert seqs == [1, 2, 3]
            assert sink.verify_chain() is True
            live = list(sink.events)
        # Reopen and verify the data persisted.
        with SQLiteAuditSink(path, resume=True) as sink2:
            assert sink2.events == live
            assert sink2.verify_chain() is True

    def test_db_schema_has_primary_key_on_seq(self, tmp_path):
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path):
            pass
        conn = sqlite3.connect(path)
        # sqlite_master has the CREATE TABLE statement.
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()[0]
        conn.close()
        assert "PRIMARY KEY" in sql.upper()
        assert "seq" in sql

    def test_fresh_sink_refuses_existing_nonempty_db(self, tmp_path):
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path) as sink:
            append_events(sink, 2)
        with pytest.raises(FileExistsError):
            SQLiteAuditSink(path)

    def test_resume_continues_sequence_and_chain(self, tmp_path):
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path) as sink:
            append_events(sink, 2)
        with SQLiteAuditSink(path, resume=True) as sink2:
            assert [e.sequence_number for e in sink2.events] == [1, 2]
            seqs = append_events(sink2, 2)
            assert seqs == [3, 4]
            assert sink2.verify_chain() is True
        # The DB itself has the right seqs in order.
        assert _db_seq_column(path) == [1, 2, 3, 4]

    def test_empty_db_resume_starts_fresh(self, tmp_path):
        # A db file created (zero events) and reopened with resume=True
        # is a legitimate empty chain.
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path):
            pass
        # The file exists but the events table is empty — resume should
        # treat it as a fresh chain.
        with SQLiteAuditSink(path, resume=True) as sink:
            assert len(sink) == 0
            assert sink.append(make_event(1)) == 1


class TestPrimaryKeyIntegrity:
    def test_duplicate_seq_rejected_at_db_layer(self, tmp_path):
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path) as sink:
            append_events(sink, 2)
        # Bypass the sink and inject a row with seq=2 (duplicate).
        conn = sqlite3.connect(path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO events (seq, line) VALUES (?, ?)",
                (2, '{"event":"x"}'),
            )
        conn.close()

    def test_out_of_order_seq_detected_by_verify_chain(self, tmp_path):
        """The DB layer catches duplicate seq at insert time, but if an
        attacker modifies a row's payload (the canonical `line` column)
        directly via SQL UPDATE, verify_chain() must catch the broken
        hash link on the next resume."""
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path) as sink:
            append_events(sink, 4)
        # Tamper with row seq=2's payload (canonical `line` column).
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT line FROM events WHERE seq = 2").fetchone()
        record = json.loads(row[0])
        record["details"] = {"n": 2, "payload": "REWRITTEN"}
        new_line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        conn.execute("UPDATE events SET line = ? WHERE seq = 2", (new_line,))
        conn.commit()
        conn.close()
        with pytest.raises(AuditStorageError, match="failed verification"):
            SQLiteAuditSink(path, resume=True)

    def test_row_deletion_detected_by_verify_chain(self, tmp_path):
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path) as sink:
            append_events(sink, 4)
        # Delete the row at seq=2 directly.
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM events WHERE seq = 2")
        conn.commit()
        conn.close()
        with pytest.raises(AuditStorageError, match="failed verification"):
            SQLiteAuditSink(path, resume=True)


class TestResumeRefusesCorruption:
    def test_resume_refuses_hash_corrupted_chain_db_untouched(self, tmp_path):
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path) as sink:
            append_events(sink, 3)
        # Tamper with the middle event's payload (parseable but wrong hash).
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT line FROM events WHERE seq = 2").fetchone()
        record = json.loads(row[0])
        record["details"] = {"n": 2, "payload": "REWRITTEN"}
        new_line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        conn.execute("UPDATE events SET line = ? WHERE seq = 2", (new_line,))
        conn.commit()
        before = os.path.getsize(path)
        with pytest.raises(AuditStorageError, match="failed verification"):
            SQLiteAuditSink(path, resume=True)
        # DB file untouched by the refusal (size may differ slightly due
        # to WAL/journal artifacts, but the events table contents are
        # preserved — verify by re-reading the tampered row).
        conn2 = sqlite3.connect(path)
        row2 = conn2.execute("SELECT line FROM events WHERE seq = 2").fetchone()
        conn2.close()
        assert json.loads(row2[0])["details"]["payload"] == "REWRITTEN"

    def test_resume_refuses_malformed_line(self, tmp_path):
        path = tmp_path / "audit.db"
        with SQLiteAuditSink(path) as sink:
            append_events(sink, 2)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE events SET line = 'not json' WHERE seq = 1")
        conn.commit()
        conn.close()
        with pytest.raises(AuditStorageError, match="malformed"):
            SQLiteAuditSink(path, resume=True)


class TestAppendFailureRollback:
    def test_malformed_event_details_rejected_at_append(self, tmp_path):
        path = tmp_path / "audit.db"
        sink = SQLiteAuditSink(path)
        bad = AuditEvent("bad", "step", "2026-01-01T00:00:00+00:00", None)
        with pytest.raises(TypeError):
            sink.append(bad)
        assert len(sink) == 0
        sink.close()
        # DB also empty.
        assert _db_seq_column(path) == []


class TestCheckpointInterop:
    def test_sqlite_sink_writes_valid_checkpoint(self, tmp_path):
        chain = tmp_path / "audit.db"
        checkpoint = tmp_path / "cp.json"
        with SQLiteAuditSink(chain) as sink:
            append_events(sink, 5)
            sink.write_checkpoint(checkpoint)
        with SQLiteAuditSink(chain, resume=True) as sink:
            assert verify_checkpoint(sink.events, checkpoint) is True

    def test_checkpoint_detects_last_event_tamper_in_sqlite(self, tmp_path):
        """Same blind-spot-closing test as for FileAuditSink: the bare
        chain's verify_chain stays True after last-event tampering, but
        the checkpoint catches it."""
        chain = tmp_path / "audit.db"
        checkpoint = tmp_path / "cp.json"
        with SQLiteAuditSink(chain) as sink:
            append_events(sink, 5)
            sink.write_checkpoint(checkpoint)
        # Tamper with the last event's payload via direct SQL.
        conn = sqlite3.connect(chain)
        row = conn.execute("SELECT line FROM events WHERE seq = 5").fetchone()
        record = json.loads(row[0])
        record["details"] = {"n": 5, "payload": "REWRITTEN"}
        new_line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        conn.execute("UPDATE events SET line = ? WHERE seq = 5", (new_line,))
        conn.commit()
        conn.close()
        # verify_chain alone is blind to last-event tampering.
        from UPMP.audit import InMemoryAuditSink
        with SQLiteAuditSink(chain, resume=True) as sink:
            pass  # we tampered prev_hash of event 5's PREDECESSOR? No —
                  # we tampered event 5's PAYLOAD, which doesn't break
                  # any prev_hash link (event 5 is the tip). Resume
                  # should succeed.
        # Reopen in raw mode and rebuild the chain.
        conn = sqlite3.connect(chain)
        rows = conn.execute("SELECT line FROM events ORDER BY seq").fetchall()
        conn.close()
        from UPMP.audit import event_from_line
        events = [event_from_line(r[0]) for r in rows]
        verifier = InMemoryAuditSink()
        verifier.events = list(events)
        assert verifier.verify_chain() is True  # blind to it
        assert verify_checkpoint(events, checkpoint) is False  # checkpoint catches


class TestEndToEndWithOrchestrator:
    def test_orchestrator_to_sqlite_to_checkpoint(self, tmp_path):
        """Full lifecycle: orchestrator writes evidence to SQLite,
        survives close/reload, verifies forensically, and is
        checkpoint-authenticated."""
        from UPMP import (ActionDispatcher, ActionResult, ActionStatus,
                          ClosedLoopOrchestrator, Constraint, FindingStatus,
                          Observation, ProcessState, VerificationPolicy,
                          WorkflowStep, RunStatus)
        from dataclasses import replace as _replace

        chain = tmp_path / "audit.db"
        checkpoint = tmp_path / "cp.json"

        def state(**values):
            obs = {k: v if isinstance(v, Observation) else Observation(v, source="test")
                   for k, v in values.items()}
            obs.setdefault("_test_guard", Observation(True, source="fixture"))
            return ProcessState("test.domain", "1", obs)

        dispatcher = ActionDispatcher({"act"})
        dispatcher.register("act", lambda req, cur: ActionResult(
            ActionStatus.SUCCEEDED, "done",
            _replace(cur, observations={**cur.observations,
                                        "done": Observation(True, source="action")})))
        step = WorkflowStep("s", "act",
                            VerificationPolicy("pre", "1",
                                (Constraint("_test_guard", "eq", True),)),
                            VerificationPolicy("post", "1",
                                (Constraint("done", "eq", True),)))

        with SQLiteAuditSink(chain) as sink:
            result = ClosedLoopOrchestrator([step], dispatcher, audit_sink=sink).run(state(done=False))
            assert result.status == RunStatus.COMPLETED
            sink.write_checkpoint(checkpoint)

        # Reopen and verify.
        with SQLiteAuditSink(chain, resume=True) as sink:
            assert len(sink) == 3
            assert [e.event for e in sink.events] == [
                "precondition_verified", "action_succeeded", "postcondition_verified"]
            assert sink.verify_chain() is True
            assert verify_checkpoint(sink.events, checkpoint) is True
