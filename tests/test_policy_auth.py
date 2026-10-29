"""M4 tests — policy signing and version lifecycle.

Covers:
  - canonical_policy determinism (same policy -> same bytes).
  - HmacPolicyAuthenticator sign/verify roundtrip.
  - Tamper detection: changing any constraint field (expected, unit,
    operator, field name), policy_id, or version breaks the signature.
  - signer_id mismatch detection.
  - Wrong-secret detection.
  - Malformed signature / non-string signature returns False (never raises).
  - sign_policy convenience wrapper.
  - Custom PolicyAuthenticator implementation (duck-typed).
  - Empty secret / empty signer_id rejected at construction.

Threat model notes:
  - The signature authenticates CONTENTS, not ORIGIN. UPMP does not
    verify that signer_id maps to a trusted key (caller responsibility).
  - HMAC with a shared secret provides authentication between parties
    that share the secret. For multi-party auth, use asymmetric signing
    (deferred to M4.1).
  - The signature does NOT prevent rollback to an older validly-signed
    policy. Version lifecycle tracking is the caller's responsibility.
"""
import pytest

from UPMP.contracts import Constraint, VerificationPolicy
from UPMP.policy_auth import (
    HmacPolicyAuthenticator,
    PolicyAuthenticator,
    SignedPolicy,
    canonical_policy,
    sign_policy,
)


def make_policy(policy_id="test", version="1", expected=10, unit="kPa", field="pressure"):
    return VerificationPolicy(policy_id, version,
                            (Constraint(field, "lte", expected, unit=unit),))


class TestCanonicalPolicy:
    def test_same_policy_same_bytes(self):
        p1 = make_policy()
        p2 = make_policy()
        assert canonical_policy(p1) == canonical_policy(p2)

    def test_different_expected_different_bytes(self):
        p1 = make_policy(expected=10)
        p2 = make_policy(expected=20)
        assert canonical_policy(p1) != canonical_policy(p2)

    def test_different_policy_id_different_bytes(self):
        p1 = make_policy(policy_id="a")
        p2 = make_policy(policy_id="b")
        assert canonical_policy(p1) != canonical_policy(p2)

    def test_different_version_different_bytes(self):
        p1 = make_policy(version="1")
        p2 = make_policy(version="2")
        assert canonical_policy(p1) != canonical_policy(p2)

    def test_different_unit_different_bytes(self):
        p1 = make_policy(unit="kPa")
        p2 = make_policy(unit="psi")
        assert canonical_policy(p1) != canonical_policy(p2)

    def test_constraint_reorder_different_bytes(self):
        """Constraints are serialized in declaration order; reordering
        them changes the canonical form (and thus the signature)."""
        c1 = Constraint("a", "eq", 1)
        c2 = Constraint("b", "eq", 2)
        p1 = VerificationPolicy("p", "1", (c1, c2))
        p2 = VerificationPolicy("p", "1", (c2, c1))
        assert canonical_policy(p1) != canonical_policy(p2)

    def test_json_is_deterministic_and_sorted(self):
        p = make_policy()
        s = canonical_policy(p)
        # sort_keys=True -> keys appear in alphabetical order.
        assert s.index('"expected"') < s.index('"field"')
        assert s.index('"field"') < s.index('"operator"')


class TestHmacPolicyAuthenticator:
    def test_sign_then_verify_roundtrip(self):
        auth = HmacPolicyAuthenticator(b"secret", signer_id="op-1")
        p = make_policy()
        sig = auth.sign(p)
        assert auth.verify(p, sig) is True

    def test_tampered_expected_detected(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = make_policy(expected=10)
        sig = auth.sign(p)
        tampered = make_policy(expected=999)
        assert auth.verify(tampered, sig) is False

    def test_tampered_unit_detected(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = make_policy(unit="kPa")
        sig = auth.sign(p)
        tampered = make_policy(unit="psi")
        assert auth.verify(tampered, sig) is False

    def test_tampered_operator_detected(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = VerificationPolicy("p", "1", (Constraint("x", "lte", 10),))
        sig = auth.sign(p)
        tampered = VerificationPolicy("p", "1", (Constraint("x", "gte", 10),))
        assert auth.verify(tampered, sig) is False

    def test_tampered_field_name_detected(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = VerificationPolicy("p", "1", (Constraint("pressure", "eq", 10),))
        sig = auth.sign(p)
        tampered = VerificationPolicy("p", "1", (Constraint("temp", "eq", 10),))
        assert auth.verify(tampered, sig) is False

    def test_tampered_policy_id_detected(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = make_policy(policy_id="original")
        sig = auth.sign(p)
        tampered = make_policy(policy_id="forged")
        assert auth.verify(tampered, sig) is False

    def test_tampered_version_detected(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = make_policy(version="1")
        sig = auth.sign(p)
        tampered = make_policy(version="2")
        assert auth.verify(tampered, sig) is False

    def test_wrong_secret_detected(self):
        auth1 = HmacPolicyAuthenticator(b"secret-1")
        auth2 = HmacPolicyAuthenticator(b"secret-2")
        p = make_policy()
        sig = auth1.sign(p)
        assert auth2.verify(p, sig) is False

    def test_malformed_signature_returns_false(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = make_policy()
        assert auth.verify(p, "not-a-hex-string") is False
        assert auth.verify(p, "") is False

    def test_non_string_signature_returns_false(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = make_policy()
        assert auth.verify(p, None) is False
        assert auth.verify(p, 12345) is False
        assert auth.verify(p, b"bytes") is False

    def test_empty_secret_rejected(self):
        with pytest.raises(ValueError, match="secret must be non-empty"):
            HmacPolicyAuthenticator(b"")
        with pytest.raises(ValueError, match="secret must be non-empty"):
            HmacPolicyAuthenticator("")

    def test_empty_signer_id_rejected(self):
        with pytest.raises(ValueError, match="signer_id must be non-empty"):
            HmacPolicyAuthenticator(b"secret", signer_id="")

    def test_string_secret_accepted_and_encoded(self):
        auth = HmacPolicyAuthenticator("string-secret")
        p = make_policy()
        sig = auth.sign(p)
        assert auth.verify(p, sig) is True


class TestSignedPolicy:
    def test_sign_policy_convenience(self):
        auth = HmacPolicyAuthenticator(b"secret", signer_id="op-1")
        p = make_policy()
        signed = sign_policy(p, auth)
        assert signed.policy is p
        assert signed.signer_id == "op-1"
        assert signed.verify(auth) is True

    def test_signed_policy_carries_signature_and_signer(self):
        auth = HmacPolicyAuthenticator(b"secret", signer_id="op-1")
        signed = sign_policy(make_policy(), auth)
        assert isinstance(signed.signature, str)
        assert len(signed.signature) == 64  # SHA-256 hex digest
        assert signed.signer_id == "op-1"
        assert signed.signed_at  # auto-populated

    def test_signer_id_mismatch_detected(self):
        auth1 = HmacPolicyAuthenticator(b"shared-secret", signer_id="op-1")
        auth2 = HmacPolicyAuthenticator(b"shared-secret", signer_id="op-2")
        # Same secret, different signer_id — signature is valid (same
        # secret), but SignedPolicy.verify checks signer_id matches.
        signed = sign_policy(make_policy(), auth1)
        # Signature itself is valid under both authenticators (same secret):
        assert auth2.verify(signed.policy, signed.signature) is True
        # But SignedPolicy.verify checks signer_id mismatch:
        assert signed.verify(auth2) is False

    def test_tampered_signed_policy_detected(self):
        auth = HmacPolicyAuthenticator(b"secret")
        signed = sign_policy(make_policy(expected=10), auth)
        # Attacker swaps in a different policy but keeps the old signature.
        tampered = SignedPolicy(
            policy=make_policy(expected=999),
            signature=signed.signature,
            signer_id=signed.signer_id,
        )
        assert tampered.verify(auth) is False


class TestCustomAuthenticator:
    def test_duck_typed_authenticator_works(self):
        """A custom class implementing sign/verify (duck-typed) works
        with SignedPolicy.verify."""

        class CustomAuth:
            def __init__(self, key):
                self.key = key
            def sign(self, policy):
                # Trivial "signature" — just for testing the Protocol.
                import hashlib
                return hashlib.sha256((canonical_policy(policy) + self.key).encode()).hexdigest()
            def verify(self, policy, signature):
                return self.sign(policy) == signature

        auth = CustomAuth("my-key")
        p = make_policy()
        sig = auth.sign(p)
        assert auth.verify(p, sig) is True
        # SignedPolicy.verify with a custom authenticator (not HmacPolicyAuthenticator)
        # skips the signer_id check and delegates to verify().
        signed = SignedPolicy(policy=p, signature=sig, signer_id="custom")
        assert signed.verify(auth) is True


class TestEmptyAndEdgeCases:
    def test_empty_constraints_policy_signs_and_verifies(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = VerificationPolicy("empty", "1", ())
        sig = auth.sign(p)
        assert auth.verify(p, sig) is True

    def test_multiple_constraints_all_covered_by_signature(self):
        auth = HmacPolicyAuthenticator(b"secret")
        p = VerificationPolicy("multi", "1", (
            Constraint("pressure", "lte", 10, unit="kPa"),
            Constraint("temperature", "between", (18, 24), unit="C"),
            Constraint("mode", "in", ("idle", "active")),
        ))
        sig = auth.sign(p)
        # Tamper any one constraint
        tampered = VerificationPolicy("multi", "1", (
            Constraint("pressure", "lte", 10, unit="kPa"),
            Constraint("temperature", "between", (18, 99), unit="C"),  # changed
            Constraint("mode", "in", ("idle", "active")),
        ))
        assert auth.verify(tampered, sig) is False

    def test_deterministic_across_processes(self):
        """The same policy + same secret always produces the same
        signature (deterministic HMAC)."""
        auth1 = HmacPolicyAuthenticator(b"secret", signer_id="op")
        auth2 = HmacPolicyAuthenticator(b"secret", signer_id="op")
        p = make_policy()
        assert auth1.sign(p) == auth2.sign(p)
