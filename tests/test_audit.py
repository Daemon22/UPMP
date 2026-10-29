"""Tests for UPMP/audit.py — hash-chained audit sink.

M1.1 (7 tests): monotonic sequence, clean chain, middle-event tamper,
  reorder, deletion, empty sink, canonical JSON determinism.

M1.2 (13 tests): first-event tamper, last-event limitation, duplication,
  sequence-number mutation, nested dicts, non-JSON-native values,
  single-event chain, range verification (partial, in-range tamper,
  pre-range tamper limitation, invalid range, out-of-bounds), and
  malformed-event rejection.

This file is purely additive — it does not modify any existing test.
All 7 M1.1 tests are preserved unchanged.
"""
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from UPMP import AuditEvent
from UPMP.audit import (
    AuditSink,
    InMemoryAuditSink,
    GENESIS_HASH,
    canonical_json,
    event_hash,
)


def _make_event(n: int = 1) -> AuditEvent:
    """Construct a test AuditEvent with index n (1..31 valid for day-of-month)."""
    return AuditEvent(
        event="test_event_%d" % n,
        step_id="step_%d" % n,
        timestamp="2026-01-%02dT00:00:00+00:00" % n,
        details={"index": n, "action": "test_action_%d" % n},
    )


def _make_chain(sink: InMemoryAuditSink, count: int) -> None:
    """Append count events to the sink."""
    for i in range(1, count + 1):
        sink.append(_make_event(i))


# ===========================================================================
#  M1.1 TESTS (7) — preserved unchanged
# ===========================================================================

class TestInMemoryAuditSink(unittest.TestCase):
    """M1.1 — InMemoryAuditSink with SHA-256 hash chaining."""

    def test_append_returns_monotonic_sequence(self):
        """append() returns 1-based monotonic sequence numbers."""
        sink = InMemoryAuditSink()
        for i in range(1, 4):
            seq = sink.append(_make_event(i))
            self.assertEqual(seq, i)
        self.assertEqual([e.sequence_number for e in sink.events], [1, 2, 3])

    def test_hash_chain_validates_clean(self):
        """A chain with no tampering validates successfully."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertEqual(len(sink), 5)
        self.assertTrue(sink.verify_chain())

    def test_tamper_detection_single_byte(self):
        """Mutating a single event's payload breaks the hash chain."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain(), "clean chain should validate before tampering")

        original = sink.events[2]
        tampered_details = dict(original.details)
        tampered_details["injected"] = "adversary_payload"
        sink.events[2] = replace(original, details=tampered_details)

        self.assertFalse(
            sink.verify_chain(),
            "chain should NOT validate after tampering event[2] payload — "
            "the next event's prev_hash no longer matches the recomputed hash",
        )

    def test_tamper_detection_sequence_reorder(self):
        """Swapping two events breaks both the hash chain and the sequence numbers."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain(), "clean chain should validate before tampering")

        sink.events[2], sink.events[3] = sink.events[3], sink.events[2]

        self.assertFalse(
            sink.verify_chain(),
            "chain should NOT validate after reordering events — "
            "prev_hash links break AND sequence_numbers become non-monotonic",
        )

    def test_tamper_detection_event_deletion(self):
        """Deleting an event breaks the hash chain (gap detectable)."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain(), "clean chain should validate before tampering")

        del sink.events[2]

        self.assertFalse(
            sink.verify_chain(),
            "chain should NOT validate after deleting an event — "
            "the prev_hash link from the new event[2] (was event[3]) "
            "points to the deleted event's hash, not to event[1]'s hash",
        )

    def test_empty_sink_validates(self):
        """A fresh sink with zero events validates as True."""
        sink = InMemoryAuditSink()
        self.assertEqual(len(sink), 0)
        self.assertTrue(sink.verify_chain(), "empty sink should validate")

    def test_canonical_json_deterministic(self):
        """Two events with the same fields but different dict insertion order
        produce the same canonical JSON (and therefore the same hash).
        """
        e1 = AuditEvent(
            event="x",
            step_id="y",
            timestamp="2026-01-01T00:00:00+00:00",
            details={"a": 1, "b": 2, "c": 3},
        )
        e2 = AuditEvent(
            event="x",
            step_id="y",
            timestamp="2026-01-01T00:00:00+00:00",
            details={"c": 3, "a": 1, "b": 2},
        )
        self.assertEqual(canonical_json(e1), canonical_json(e2))
        self.assertEqual(
            event_hash(GENESIS_HASH, e1),
            event_hash(GENESIS_HASH, e2),
        )


# ===========================================================================
#  M1.2 TESTS (13) — verification hardening
# ===========================================================================

class TestTamperDetectionExtended(unittest.TestCase):
    """M1.2 — extended tamper detection covering first/last events, duplication,
    and sequence-number mutation."""

    def test_first_event_tamper_detected(self):
        """Tampering the FIRST event is detected because event[1].prev_hash
        was computed from event[0]'s original payload — it no longer matches
        the recomputed hash of the tampered event[0]."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain())

        # Tamper event[0] (sequence_number 1) — modify its details
        original = sink.events[0]
        tampered_details = dict(original.details)
        tampered_details["adversary"] = "injected_at_genesis"
        sink.events[0] = replace(original, details=tampered_details)

        self.assertFalse(
            sink.verify_chain(),
            "chain should NOT validate after tampering event[0] — "
            "event[1].prev_hash (computed from original event[0]) "
            "no longer matches the recomputed hash of tampered event[0]",
        )

    def test_last_event_tamper_NOT_detected_limitation(self):
        """DOCUMENTED LIMITATION: tampering the LAST event's payload is NOT
        detected by verify_chain() alone, because there is no downstream
        event whose prev_hash would catch the mismatch.

        This test PASSES (verify_chain returns True) — it documents the
        limitation, not a desired behavior. Detecting last-event tampering
        requires an external anchor (signed checkpoint committing to the
        last event's hash). This is out of scope for M1.x.
        """
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain())

        # Tamper the LAST event (index 4, sequence_number 5)
        original = sink.events[4]
        tampered_details = dict(original.details)
        tampered_details["adversary"] = "injected_at_tail"
        sink.events[4] = replace(original, details=tampered_details)

        # The chain STILL validates — this is the limitation.
        # The tampered event[4] has no successor to catch the mismatch.
        self.assertTrue(
            sink.verify_chain(),
            "KNOWN LIMITATION: tampering the last event is not detected "
            "by verify_chain() alone. An external anchor (signed checkpoint) "
            "is required to authenticate the last event. See audit.py module "
            "docstring for details.",
        )

    def test_event_duplication_detected(self):
        """Duplicating an event breaks the sequence_number monotonicity check."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain())

        # Duplicate event at index 2 (insert a copy right after it)
        sink.events.insert(3, sink.events[2])

        self.assertFalse(
            sink.verify_chain(),
            "chain should NOT validate after duplicating an event — "
            "sequence_numbers become non-monotonic (two events with seq 3) "
            "AND prev_hash links break",
        )

    def test_sequence_number_mutation_detected(self):
        """Mutating an event's sequence_number breaks the monotonicity check."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain())

        # Mutate event[2]'s sequence_number from 3 to 99
        original = sink.events[2]
        sink.events[2] = replace(original, sequence_number=99)

        self.assertFalse(
            sink.verify_chain(),
            "chain should NOT validate after mutating a sequence_number — "
            "the monotonicity check (sequence_number == i+1) fails",
        )


class TestCanonicalJsonExtended(unittest.TestCase):
    """M1.2 — canonical JSON with nested dicts and non-JSON-native values."""

    def test_nested_dicts_canonical_json(self):
        """Nested dictionaries with different key insertion order produce
        the same canonical JSON. sort_keys=True applies recursively."""
        e1 = AuditEvent(
            event="x",
            step_id="y",
            timestamp="2026-01-01T00:00:00+00:00",
            details={"outer": {"z": 3, "a": 1, "m": 2}, "flat": 42},
        )
        e2 = AuditEvent(
            event="x",
            step_id="y",
            timestamp="2026-01-01T00:00:00+00:00",
            details={"flat": 42, "outer": {"a": 1, "m": 2, "z": 3}},
        )
        self.assertEqual(canonical_json(e1), canonical_json(e2))
        self.assertEqual(
            event_hash(GENESIS_HASH, e1),
            event_hash(GENESIS_HASH, e2),
        )

    def test_non_json_native_values_deterministic(self):
        """Non-JSON-native values (datetime, custom objects) are handled by
        default=str. The same object always produces the same hash."""
        dt = datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)

        class CustomObj:
            """A non-JSON-serializable custom object."""
            def __str__(self):
                return "CustomObj(token=abc123)"

        custom = CustomObj()

        e1 = AuditEvent(
            event="x",
            step_id="y",
            timestamp="2026-01-01T00:00:00+00:00",
            details={"when": dt, "what": custom, "count": 7},
        )
        e2 = AuditEvent(
            event="x",
            step_id="y",
            timestamp="2026-01-01T00:00:00+00:00",
            details={"count": 7, "what": custom, "when": dt},
        )
        # Same values, different key order → same canonical JSON
        self.assertEqual(canonical_json(e1), canonical_json(e2))
        self.assertEqual(
            event_hash(GENESIS_HASH, e1),
            event_hash(GENESIS_HASH, e2),
        )
        # Verify the serialization actually happened (not empty)
        self.assertIn("CustomObj", canonical_json(e1))
        self.assertIn("2026", canonical_json(e1))

    def test_single_event_chain_validates(self):
        """A chain with exactly one event validates successfully."""
        sink = InMemoryAuditSink()
        sink.append(_make_event(1))
        self.assertEqual(len(sink), 1)
        self.assertTrue(sink.verify_chain())


class TestRangeVerification(unittest.TestCase):
    """M1.2 — start_seq / end_seq range verification."""

    def test_range_verification_partial_clean(self):
        """verify_chain(start_seq=2, end_seq=4) on a clean 5-event chain
        returns True. The range [2,4] is internally consistent and correctly
        anchored to event[0] (the event before the range)."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain(start_seq=2, end_seq=4))

    def test_range_verification_tamper_in_range_detected(self):
        """Tampering an event WITHIN the verified range is detected."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain())

        # Tamper event[2] (sequence_number 3, within range [2,4])
        original = sink.events[2]
        sink.events[2] = replace(original, details={**original.details, "x": 1})

        self.assertFalse(
            sink.verify_chain(start_seq=2, end_seq=4),
            "range verification should detect tampering within the range",
        )

    def test_range_verification_tamper_before_range_NOT_detected(self):
        """DOCUMENTED LIMITATION: tampering an event BEFORE the verified range
        is NOT detected by range verification. Events before start_seq are
        trusted as the anchor.

        This test PASSES (range verification returns True) — it documents
        the limitation. For full verification, call verify_chain() with no
        arguments.
        """
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain())

        # Tamper event[0] (before the range [3,5])
        original = sink.events[0]
        sink.events[0] = replace(original, details={**original.details, "x": 1})

        # Full-chain verification catches it
        self.assertFalse(sink.verify_chain(), "full verification should catch event[0] tamper")

        # Range verification starting at 3 does NOT catch it (event[0] is trusted as anchor's predecessor)
        # Wait — actually, the anchor is events[start_seq-2] = events[1]. If event[0]
        # is tampered, events[1].prev_hash still matches the ORIGINAL event[0]'s hash.
        # But we recompute event_hash(events[1].prev_hash, events[1]) — this uses
        # events[1]'s prev_hash (which is the original hash of event[0]) and events[1]'s
        # payload (unchanged). So the recomputed anchor hash matches events[2].prev_hash.
        # The tampered event[0] is NOT detected by range [3,5] verification.
        self.assertTrue(
            sink.verify_chain(start_seq=3, end_seq=5),
            "KNOWN LIMITATION: range verification does not detect tampering "
            "before the anchor. event[0] is before the range [3,5] and its "
            "tamper is not caught. Use full verify_chain() for complete verification.",
        )

    def test_range_verification_invalid_range_returns_true(self):
        """An invalid range (start_seq > end_seq) is treated as an empty
        range — nothing to verify, returns True."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain(start_seq=4, end_seq=2))

    def test_range_verification_out_of_bounds_returns_true(self):
        """A range entirely beyond the chain length is empty — returns True."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain(start_seq=10, end_seq=20))
        # Also: start_seq beyond the chain, end_seq=None
        self.assertTrue(sink.verify_chain(start_seq=10))


class TestMalformedEvents(unittest.TestCase):
    """M1.2 — malformed events must not silently pass verification."""

    def test_malformed_event_details_none_does_not_silently_pass(self):
        """An event with details=None causes canonical_json to raise TypeError.
        verify_chain() must catch this and return False — it must NOT crash
        the verifier (DoS vector) and must NOT silently pass."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain())

        # Replace event[2] with one that has details=None
        sink.events[2] = AuditEvent(
            event="malformed",
            step_id="step_3",
            timestamp="2026-01-03T00:00:00+00:00",
            details=None,  # Malformed — canonical_json will raise TypeError
        )

        # verify_chain must return False, not raise
        result = sink.verify_chain()
        self.assertFalse(
            result,
            "malformed event (details=None) must not silently pass — "
            "verify_chain must return False, not crash or return True",
        )

    def test_malformed_event_missing_event_field_does_not_silently_pass(self):
        """An event with event=None causes json.dumps to produce 'null' —
        this is technically valid JSON but produces a different hash than
        the original event, so the chain breaks. verify_chain returns False."""
        sink = InMemoryAuditSink()
        _make_chain(sink, 5)
        self.assertTrue(sink.verify_chain())

        # Replace event[2] with one that has event=None (malformed but JSON-serializable)
        original = sink.events[2]
        sink.events[2] = replace(original, event=None)

        # The hash of the tampered event changes, breaking the chain
        self.assertFalse(
            sink.verify_chain(),
            "malformed event (event=None) changes the hash — "
            "chain should NOT validate",
        )


if __name__ == "__main__":
    unittest.main()
