"""M4.1 tests — Ed25519PolicyAuthenticator (asymmetric policy signing).

Covers:
  - generate_ed25519_keypair() produces valid PEM bytes.
  - Signer-mode authenticator (private key) can sign and verify.
  - Verifier-mode authenticator (public key only) can verify but not sign.
  - Private key derives the public key automatically.
  - sign/verify roundtrip with SignedPolicy.
  - Tamper detection: any field change breaks the signature.
  - Wrong key pair: signature from one key fails under another.
  - Malformed signature / non-string returns False (never raises).
  - signer_id mismatch detection (via SignedPolicy.verify).
  - Cross-implementation compatibility: a policy signed by
    Ed25519PolicyAuthenticator is NOT verifiable by HmacPolicyAuthenticator
    (different signing primitives) — but the SignedPolicy.verify method
    correctly routes to the right authenticator type.
  - Determinism: same key + same policy -> same signature (Ed25519 is
    deterministic, unlike ECDSA which adds random k).

Dependency:
  - Requires the `cryptography` package. If not installed, all tests
    are skipped (skipif marker).
"""
import pytest

try:
    import cryptography  # noqa: F401
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

pytestmark = pytest.mark.skipif(not _HAS_CRYPTO,
                                 reason="requires the 'cryptography' package")

from UPMP.contracts import Constraint, VerificationPolicy
from UPMP.policy_auth import (
    HmacPolicyAuthenticator, SignedPolicy, canonical_policy, sign_policy,
)
from UPMP.asymmetric_policy_auth import (
    Ed25519PolicyAuthenticator, generate_ed25519_keypair,
)


def make_policy(policy_id="test", version="1", expected=10, unit="kPa"):
    return VerificationPolicy(policy_id, version,
                            (Constraint("pressure", "lte", expected, unit=unit),))


@pytest.fixture(scope="module")
def keypair():
    """Generate one keypair per test module (key generation is slow-ish)."""
    return generate_ed25519_keypair()


@pytest.fixture
def signer(keypair):
    priv_pem, _ = keypair
    return Ed25519PolicyAuthenticator(private_key_pem=priv_pem, signer_id="authority")


@pytest.fixture
def verifier(keypair):
    _, pub_pem = keypair
    return Ed25519PolicyAuthenticator(public_key_pem=pub_pem, signer_id="authority")


class TestKeypairGeneration:
    def test_generate_returns_two_pem_byte_strings(self):
        priv, pub = generate_ed25519_keypair()
        assert isinstance(priv, bytes)
        assert isinstance(pub, bytes)
        assert priv.startswith(b"-----BEGIN PRIVATE KEY-----")
        assert pub.startswith(b"-----BEGIN PUBLIC KEY-----")

    def test_generated_keys_are_unique(self):
        priv1, pub1 = generate_ed25519_keypair()
        priv2, pub2 = generate_ed25519_keypair()
        assert priv1 != priv2
        assert pub1 != pub2


class TestSignerMode:
    def test_signer_can_sign(self, signer):
        p = make_policy()
        sig = signer.sign(p)
        assert isinstance(sig, str)
        # Ed25519 signatures are 64 bytes -> 128 hex chars.
        assert len(sig) == 128

    def test_signer_can_verify(self, signer):
        p = make_policy()
        sig = signer.sign(p)
        assert signer.verify(p, sig) is True

    def test_signer_is_signer_property(self, signer):
        assert signer.is_signer is True

    def test_signer_has_public_key_pem(self, signer):
        pem = signer.public_key_pem
        assert pem.startswith(b"-----BEGIN PUBLIC KEY-----")


class TestVerifierMode:
    def test_verifier_can_verify(self, signer, verifier):
        p = make_policy()
        sig = signer.sign(p)
        assert verifier.verify(p, sig) is True

    def test_verifier_cannot_sign(self, verifier):
        p = make_policy()
        with pytest.raises(TypeError, match="verifier-only"):
            verifier.sign(p)

    def test_verifier_is_signer_false(self, verifier):
        assert verifier.is_signer is False

    def test_verifier_has_public_key_pem(self, verifier):
        assert verifier.public_key_pem.startswith(b"-----BEGIN PUBLIC KEY-----")


class TestPrivateKeyDerivesPublic:
    def test_private_only_constructor_derives_public(self, keypair):
        priv_pem, expected_pub_pem = keypair
        auth = Ed25519PolicyAuthenticator(private_key_pem=priv_pem, signer_id="x")
        # The derived public key should match the one from generate.
        assert auth.public_key_pem == expected_pub_pem


class TestTamperDetection:
    def test_tampered_expected_detected(self, signer, verifier):
        p = make_policy(expected=10)
        sig = signer.sign(p)
        tampered = make_policy(expected=999)
        assert verifier.verify(tampered, sig) is False

    def test_tampered_unit_detected(self, signer, verifier):
        p = make_policy(unit="kPa")
        sig = signer.sign(p)
        tampered = make_policy(unit="psi")
        assert verifier.verify(tampered, sig) is False

    def test_tampered_policy_id_detected(self, signer, verifier):
        p = make_policy(policy_id="original")
        sig = signer.sign(p)
        tampered = make_policy(policy_id="forged")
        assert verifier.verify(tampered, sig) is False

    def test_tampered_version_detected(self, signer, verifier):
        p = make_policy(version="1")
        sig = signer.sign(p)
        tampered = make_policy(version="2")
        assert verifier.verify(tampered, sig) is False

    def test_tampered_operator_detected(self, signer, verifier):
        p = VerificationPolicy("p", "1", (Constraint("x", "lte", 10),))
        sig = signer.sign(p)
        tampered = VerificationPolicy("p", "1", (Constraint("x", "gte", 10),))
        assert verifier.verify(tampered, sig) is False

    def test_tampered_field_name_detected(self, signer, verifier):
        p = VerificationPolicy("p", "1", (Constraint("pressure", "eq", 10),))
        sig = signer.sign(p)
        tampered = VerificationPolicy("p", "1", (Constraint("temp", "eq", 10),))
        assert verifier.verify(tampered, sig) is False


class TestWrongKeyPair:
    def test_signature_under_wrong_key_fails(self):
        priv1, _ = generate_ed25519_keypair()
        _, pub2 = generate_ed25519_keypair()
        signer1 = Ed25519PolicyAuthenticator(private_key_pem=priv1, signer_id="a")
        verifier2 = Ed25519PolicyAuthenticator(public_key_pem=pub2, signer_id="a")
        p = make_policy()
        sig = signer1.sign(p)
        # The signature was made with key 1; verifying with key 2 fails.
        assert verifier2.verify(p, sig) is False


class TestMalformedInput:
    def test_non_string_signature_returns_false(self, verifier):
        p = make_policy()
        assert verifier.verify(p, None) is False
        assert verifier.verify(p, 12345) is False
        assert verifier.verify(p, b"bytes") is False

    def test_non_hex_signature_returns_false(self, verifier):
        p = make_policy()
        assert verifier.verify(p, "not-hex-zzz") is False
        assert verifier.verify(p, "") is False

    def test_wrong_length_hex_returns_false(self, verifier):
        p = make_policy()
        # Valid hex but wrong length (64 chars instead of 128).
        assert verifier.verify(p, "a" * 64) is False


class TestSignedPolicyIntegration:
    def test_sign_policy_convenience(self, signer):
        p = make_policy()
        signed = sign_policy(p, signer)
        assert signed.policy is p
        assert signed.signer_id == "authority"
        assert len(signed.signature) == 128

    def test_signed_policy_verify_with_signer(self, signer):
        signed = sign_policy(make_policy(), signer)
        assert signed.verify(signer) is True

    def test_signed_policy_verify_with_verifier(self, signer, verifier):
        signed = sign_policy(make_policy(), signer)
        assert signed.verify(verifier) is True

    def test_signed_policy_signer_id_mismatch(self, signer):
        priv2, _ = generate_ed25519_keypair()
        # Same key bytes but different signer_id -> SignedPolicy.verify
        # checks signer_id matches the authenticator's.
        auth_diff_id = Ed25519PolicyAuthenticator(private_key_pem=priv2, signer_id="other")
        # Note: priv2 is a DIFFERENT key, so this tests both wrong-key AND
        # signer_id mismatch. Let's test signer_id mismatch with the SAME key.
        auth_same_key_diff_id = Ed25519PolicyAuthenticator(
            private_key_pem=signer._private_key_pem, signer_id="other")
        signed = sign_policy(make_policy(), signer)  # signer_id="authority"
        # Same key, but auth has signer_id="other" -> mismatch.
        assert signed.verify(auth_same_key_diff_id) is False


class TestDeterminism:
    def test_same_key_same_policy_same_signature(self, signer):
        """Ed25519 is deterministic (unlike ECDSA with random k).
        Same key + same policy -> same signature, every time."""
        p = make_policy()
        sig1 = signer.sign(p)
        sig2 = signer.sign(p)
        assert sig1 == sig2

    def test_different_signers_different_signatures(self):
        priv1, _ = generate_ed25519_keypair()
        priv2, _ = generate_ed25519_keypair()
        s1 = Ed25519PolicyAuthenticator(private_key_pem=priv1, signer_id="a")
        s2 = Ed25519PolicyAuthenticator(private_key_pem=priv2, signer_id="b")
        p = make_policy()
        assert s1.sign(p) != s2.sign(p)


class TestCrossImplementationCompatibility:
    def test_ed25519_signed_policy_not_verifiable_by_hmac(self, signer):
        """A policy signed by Ed25519 cannot be verified by HMAC
        (different primitives). This is by design — the SignedPolicy.verify
        method delegates to the authenticator's verify, which checks
        the signature against its own primitive."""
        hmac_auth = HmacPolicyAuthenticator(b"some-secret", signer_id="authority")
        signed = sign_policy(make_policy(), signer)  # Ed25519 signature
        # HMAC authenticator will compute an HMAC and compare — won't match.
        assert signed.verify(hmac_auth) is False

    def test_hmac_signed_policy_not_verifiable_by_ed25519(self):
        """And vice versa."""
        hmac_auth = HmacPolicyAuthenticator(b"some-secret", signer_id="authority")
        priv, _ = generate_ed25519_keypair()
        ed_auth = Ed25519PolicyAuthenticator(private_key_pem=priv, signer_id="authority")
        signed = sign_policy(make_policy(), hmac_auth)  # HMAC signature
        # Ed25519 will try to verify an HMAC signature -> fails.
        assert signed.verify(ed_auth) is False


class TestConstructionValidation:
    def test_no_keys_rejected(self):
        with pytest.raises(ValueError, match="must provide"):
            Ed25519PolicyAuthenticator()

    def test_empty_signer_id_rejected(self, keypair):
        priv, _ = keypair
        with pytest.raises(ValueError, match="signer_id must be non-empty"):
            Ed25519PolicyAuthenticator(private_key_pem=priv, signer_id="")
