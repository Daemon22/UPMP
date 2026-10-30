"""M1.3 tests — durable audit storage, recovery model, trusted checkpoint.

Covers: FileAuditSink roundtrip and resume, torn-write crash recovery,
corruption refusal, append-failure rollback, forensic verification, and
checkpoint-based detection of last-event tampering and truncation.

Security-limitation tests are marked as such in their docstrings: they
assert DOCUMENTED behavior (what the mechanism does NOT provide), not
desired end-state guarantees.
"""
import json
import os
from dataclasses import replace

import pytest

from UPMP.audit import (
    AuditStorageError,
    CHECKPOINT_FORMAT,
    CHECKPOINT_VERSION,
    FileAuditSink,
    InMemoryAuditSink,
    event_from_line,
    event_to_line,
    load_audit_file,
    verify_audit_file,
    verify_checkpoint,
    write_checkpoint,
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


def read_lines(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def rewrite_file(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")


class TestFileAuditSinkBasics:
    def test_append_persists_and_roundtrips_exactly(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        with FileAuditSink(path) as sink:
            seqs = append_events(sink, 3)
            assert seqs == [1, 2, 3]
            live = list(sink.events)
        loaded = load_audit_file(path)
        assert loaded == live  # payload + chain metadata all roundtrip
        assert verify_audit_file(path) is True

    def test_jsonl_format_one_event_per_line(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        with FileAuditSink(path) as sink:
            append_events(sink, 3)
        lines = read_lines(path)
        assert len(lines) == 3
        for i, line in enumerate(lines, start=1):
            record = json.loads(line)
            for key in ("event", "step_id", "timestamp", "details",
                        "sequence_number", "prev_hash"):
                assert key in record
            assert record["sequence_number"] == i

    def test_verify_chain_live_sink(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        sink = FileAuditSink(path)
        assert sink.verify_chain() is True  # empty chain is valid
        append_events(sink, 5)
        assert sink.verify_chain() is True
        assert len(sink) == 5
        sink.close()

    def test_fresh_sink_refuses_existing_nonempty_file(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        with FileAuditSink(path) as sink:
            append_events(sink, 2)
        with pytest.raises(FileExistsError):
            FileAuditSink(path)

    def test_resume_continues_sequence_and_chain(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        with FileAuditSink(path) as sink:
            append_events(sink, 2)
        with FileAuditSink(path, resume=True) as sink2:
            assert [e.sequence_number for e in sink2.events] == [1, 2]
            seqs = append_events(sink2, 2)
            assert seqs == [3, 4]
            assert sink2.verify_chain() is True
        assert len(load_audit_file(path)) == 4
        assert verify_audit_file(path) is True

    def test_resume_on_empty_file_starts_fresh(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        path.write_text("")  # empty file: legitimate empty chain
        with FileAuditSink(path, resume=True) as sink:
            assert append_events(sink, 1) == [1]
            assert sink.verify_chain() is True

    def test_empty_chain_file_is_valid(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        with FileAuditSink(path):
            pass
        assert load_audit_file(path) == []
        assert verify_audit_file(path) is True


class TestRecoveryModel:
    def _file_with_events(self, tmp_path, count=3):
        path = tmp_path / "audit.jsonl"
        with FileAuditSink(path) as sink:
            append_events(sink, count)
        return path

    def test_strict_load_raises_on_torn_final_line(self, tmp_path):
        path = self._file_with_events(tmp_path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"event": "tam')  # simulate torn final write
        with pytest.raises(AuditStorageError, match="line 4"):
            load_audit_file(path, strict=True)

    def test_crash_recovery_drops_single_torn_final_record(self, tmp_path):
        path = self._file_with_events(tmp_path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"event": "tam')
        events = load_audit_file(path, strict=False)
        assert len(events) == 3
        # And a resumed sink physically removes the torn tail.
        with FileAuditSink(path, resume=True) as sink:
            assert len(sink) == 3
            append_events(sink, 1)
        assert os.path.getsize(path) == sum(
            len(event_to_line(e).encode("utf-8")) + 1 for e in load_audit_file(path))
        assert verify_audit_file(path) is True

    def test_non_final_malformed_record_raises_even_in_crash_mode(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        with FileAuditSink(path) as sink:
            append_events(sink, 3)
        lines = read_lines(path)
        lines[1] = 'not json at all'  # corrupt a NON-final record
        rewrite_file(path, lines)
        with pytest.raises(AuditStorageError, match="line 2"):
            load_audit_file(path, strict=False)
        with pytest.raises(AuditStorageError):
            load_audit_file(path, strict=True)

    def test_two_trailing_malformed_records_raise_even_in_crash_mode(self, tmp_path):
        # A torn write cannot produce two malformed trailing records.
        path = self._file_with_events(tmp_path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"event": "tam\n{"event": "als')
        with pytest.raises(AuditStorageError):
            load_audit_file(path, strict=False)

    def test_resume_refuses_hash_corrupted_chain_and_leaves_file_untouched(self, tmp_path):
        path = self._file_with_events(tmp_path)
        before = path.read_bytes()
        lines = read_lines(path)
        record = json.loads(lines[1])
        record["details"] = {"n": 2, "payload": "REWRITTEN"}  # parseable, wrong hash
        lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        rewrite_file(path, lines)
        tampered = path.read_bytes()
        assert tampered != before
        with pytest.raises(AuditStorageError, match="failed verification"):
            FileAuditSink(path, resume=True)
        assert path.read_bytes() == tampered  # untouched by the refusal

    def test_verify_audit_file_detects_on_disk_tampering(self, tmp_path):
        path = self._file_with_events(tmp_path)
        assert verify_audit_file(path) is True
        lines = read_lines(path)
        record = json.loads(lines[0])
        record["event"] = "evt_REWRITTEN"
        lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        rewrite_file(path, lines)
        assert verify_audit_file(path) is False

    def test_blank_lines_at_eof_ignored_blank_elsewhere_malformed(self, tmp_path):
        path = self._file_with_events(tmp_path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n\n")
        assert len(load_audit_file(path)) == 3
        lines = read_lines(path) + ["\n\n"]
        lines.insert(1, "")  # blank line between records
        rewrite_file(path, lines)
        with pytest.raises(AuditStorageError):
            load_audit_file(path, strict=True)


class TestAppendFailureRollback:
    def test_failed_append_rolls_back_memory_and_disk(self, tmp_path, monkeypatch):
        path = tmp_path / "audit.jsonl"
        real_fsync = os.fsync
        state = {"fail": False}

        def flaky_fsync(fd):
            if state["fail"]:
                raise OSError("simulated disk failure")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", flaky_fsync)
        sink = FileAuditSink(path)
        append_events(sink, 2)
        state["fail"] = True
        with pytest.raises(OSError, match="simulated disk failure"):
            sink.append(make_event(99))
        state["fail"] = False
        # Memory and disk agree: the failed event was NOT recorded.
        assert len(sink) == 2
        assert [e.sequence_number for e in sink.events] == [1, 2]
        assert sink.verify_chain() is True
        # The next append lands on a clean prefix (no torn line beneath it).
        assert sink.append(make_event(3)) == 3
        sink.close()
        assert verify_audit_file(path) is True
        assert len(load_audit_file(path)) == 3

    def test_malformed_event_details_rejected_at_append(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        sink = FileAuditSink(path)
        bad = AuditEvent("bad", "step", "2026-01-01T00:00:00+00:00", None)
        with pytest.raises(TypeError):
            sink.append(bad)
        assert len(sink) == 0  # nothing recorded, chain unpolluted
        sink.close()
        assert load_audit_file(path) == []


class TestLineFormat:
    def test_event_line_roundtrip(self, tmp_path):
        event = AuditEvent("evt", "step", "2026-01-01T00:00:00+00:00", {"k": [1, 2, {"x": "y"}]})
        line = event_to_line(event)
        assert json.loads(line)["event"] == "evt"
        loaded = event_from_line(line)
        assert loaded == event

    def test_sequence_metadata_roundtrip(self, tmp_path):
        event = AuditEvent("evt", "step", "2026-01-01T00:00:00+00:00", {},
                           sequence_number=7, prev_hash="abc123")
        loaded = event_from_line(event_to_line(event))
        assert loaded.sequence_number == 7
        assert loaded.prev_hash == "abc123"

    def test_non_json_native_details_hash_stable_across_persistence(self, tmp_path):
        # A datetime value is stringified identically by canonical_json
        # (hashing) and event_to_line (persistence), so the hash computed
        # at append time remains verifiable after a reload.
        from datetime import datetime, timezone
        from UPMP.audit import canonical_json, event_hash
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        event = AuditEvent("evt", "step", "2026-01-01T00:00:00+00:00", {"at": stamp})
        path = tmp_path / "audit.jsonl"
        with FileAuditSink(path) as sink:
            sink.append(event)
        loaded = load_audit_file(path)[0]
        assert isinstance(loaded.details["at"], str)  # documented coercion
        assert event_hash(loaded.prev_hash, loaded) == event_hash(event.prev_hash, event)
        assert canonical_json(loaded) == canonical_json(event)


class TestTrustedCheckpoint:
    def _sink_and_checkpoint(self, tmp_path, count=5):
        chain = tmp_path / "audit.jsonl"
        checkpoint = tmp_path / "checkpoint.json"
        with FileAuditSink(chain) as sink:
            append_events(sink, count)
            sink.write_checkpoint(checkpoint)
        return chain, checkpoint

    def test_checkpoint_roundtrip(self, tmp_path):
        chain, checkpoint = self._sink_and_checkpoint(tmp_path)
        assert verify_checkpoint(load_audit_file(chain), checkpoint) is True

    def test_checkpoint_record_shape(self, tmp_path):
        _, checkpoint = self._sink_and_checkpoint(tmp_path)
        record = json.loads(checkpoint.read_text())
        assert record["format"] == CHECKPOINT_FORMAT
        assert record["version"] == CHECKPOINT_VERSION
        assert record["chain_length"] == 5
        assert len(record["tip_hash"]) == 64  # SHA-256 hex digest

    def test_checkpoint_detects_LAST_event_tamper(self, tmp_path):
        """THE M1.3 capability: the bare hash chain cannot see last-event
        tampering (verify_chain stays True), the checkpoint catches it."""
        chain, checkpoint = self._sink_and_checkpoint(tmp_path)
        events = load_audit_file(chain)
        verifier = InMemoryAuditSink()
        verifier.events = list(events)
        assert verifier.verify_chain() is True  # internal check: blind to it

        tampered = list(events[:-1]) + [replace(events[-1], details={"n": 5, "payload": "REWRITTEN"})]
        rewrite_file(chain, [event_to_line(e) for e in tampered])
        reloaded = load_audit_file(chain)
        verifier2 = InMemoryAuditSink()
        verifier2.events = list(reloaded)
        assert verifier2.verify_chain() is True  # still internally consistent!
        assert verify_checkpoint(reloaded, checkpoint) is False  # checkpoint catches it

    def test_checkpoint_detects_truncation(self, tmp_path):
        chain, checkpoint = self._sink_and_checkpoint(tmp_path)
        events = load_audit_file(chain)
        rewrite_file(chain, [event_to_line(e) for e in events[:3]])
        truncated = load_audit_file(chain)
        assert verify_audit_file(chain) is True  # the prefix is a valid chain
        assert verify_checkpoint(truncated, checkpoint) is False  # length + tip mismatch

    def test_checkpoint_detects_mid_chain_tamper(self, tmp_path):
        chain, checkpoint = self._sink_and_checkpoint(tmp_path)
        events = load_audit_file(chain)
        tampered = list(events)
        tampered[2] = replace(tampered[2], details={"n": 3, "payload": "REWRITTEN"})
        assert verify_checkpoint(tampered, checkpoint) is False

    def test_checkpoint_detects_reorder(self, tmp_path):
        chain, checkpoint = self._sink_and_checkpoint(tmp_path)
        events = load_audit_file(chain)
        reordered = [events[1], events[0]] + events[2:]
        assert verify_checkpoint(reordered, checkpoint) is False

    def test_checkpoint_malformed_or_missing_returns_false(self, tmp_path):
        chain, checkpoint = self._sink_and_checkpoint(tmp_path)
        events = load_audit_file(chain)
        bad = tmp_path / "bad.json"
        bad.write_text("not json{")
        assert verify_checkpoint(events, bad) is False
        assert verify_checkpoint(events, tmp_path / "missing.json") is False
        wrong_format = tmp_path / "wrong.json"
        wrong_format.write_text(json.dumps({"format": "other", "version": 1,
                                            "chain_length": 5, "tip_hash": "x"}))
        assert verify_checkpoint(events, wrong_format) is False

    def test_checkpoint_refuses_invalid_chain(self, tmp_path):
        events = [make_event(1), make_event(2)]
        broken = [events[0], replace(events[1], prev_hash="forged")]
        with pytest.raises(AuditStorageError, match="refusing to checkpoint"):
            write_checkpoint(broken, tmp_path / "cp.json")

    def test_empty_chain_checkpoint(self, tmp_path):
        cp = tmp_path / "cp.json"
        write_checkpoint([], cp)
        assert verify_checkpoint([], cp) is True
        assert verify_checkpoint([make_event(1)], cp) is False

    def test_module_level_checkpoint_matches_sink_convenience(self, tmp_path):
        chain = tmp_path / "a.jsonl"
        cp1, cp2 = tmp_path / "c1.json", tmp_path / "c2.json"
        with FileAuditSink(chain) as sink:
            append_events(sink, 4)
            sink.write_checkpoint(cp1, created_at="2026-01-01T00:00:00+00:00")
        write_checkpoint(load_audit_file(chain), cp2, created_at="2026-01-01T00:00:00+00:00")
        assert json.loads(cp1.read_text())["tip_hash"] == json.loads(cp2.read_text())["tip_hash"]
