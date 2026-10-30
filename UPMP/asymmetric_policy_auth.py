"""Asymmetric policy signing for UPMP — Ed25519 implementation.

M4.1 scope — provides asymmetric (public-key) signing for
VerificationPolicy objects, complementing the HMAC-based HmacPolicyAuthenticator
(M4). With Ed25519, the signer holds a private key and verifiers hold
the corresponding public key — multiple verifiers can verify without
sharing the signing secret.

THREAT.md context:
  - M4 (HmacPolicyAuthenticator) provides content authentication
    between parties that share a secret. M4.1 (Ed25519PolicyAuthenticator)
    provides content authentication between parties that share only a
    PUBLIC key. The signer's private key never leaves the signer.
  - This closes the M4 limitation: "HMAC with a shared secret provides
    authentication between parties that share the secret. For multi-party
    authentication, use asymmetric signing."

Design:
  - Ed25519PolicyAuthenticator implements the PolicyAuthenticator Protocol
    from UPMP/policy_auth.py. It uses cryptography.hazmat.primitives.asymmetric.ed25519.
  - The authenticator stores raw key BYTES (not key objects) so it is
    picklable and serializable. Key objects are reconstructed on demand.
  - sign(policy) uses the private key to produce an Ed25519 signature
    over canonical_policy(policy). Returns the signature as hex.
  - verify(policy, signature) uses the public key to verify. Returns
    True/False (never raises — catches InvalidSignature and malformed input).
  - A signer-only authenticator (private key) can both sign and verify.
    A verifier-only authenticator (public key) can only verify; sign()
    raises TypeError.
  - generate_ed25519_keypair() returns (private_bytes, public_bytes)
    in PEM format for storage.

Dependencies:
  - Requires the `cryptography` package (pip install cryptography).
  - If cryptography is not installed, importing this module raises
    ImportError with a helpful message.

Backward compatibility:
  - Pure addition. HmacPolicyAuthenticator (M4) is unchanged. The
    PolicyAuthenticator Protocol accepts both implementations. Callers
    choose based on their trust model (shared secret vs public key).
"""
from __future__ import annotations

from typing import Optional, Tuple, Union

from .contracts import VerificationPolicy
from .policy_auth import canonical_policy

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key, load_pem_public_key,
    )
    from cryptography.exceptions import InvalidSignature
    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    _CRYPTOGRAPHY_AVAILABLE = False


def _require_cryptography():
    if not _CRYPTOGRAPHY_AVAILABLE:
        raise ImportError(
            "UPMP.asymmetric_policy_auth requires the 'cryptography' package. "
            "Install it with: pip install cryptography"
        )


def generate_ed25519_keypair() -> Tuple[bytes, bytes]:
    """Generate a new Ed25519 keypair.

    Returns:
        (private_key_pem_bytes, public_key_pem_bytes) — both in PEM
        format, suitable for storage in a secrets vault / KMS.

    The private key PEM is encrypted-at-rest by the caller's storage
    mechanism (UPMP does not encrypt PEM files — that's the deployment's
    responsibility).
    """
    _require_cryptography()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),  # caller encrypts at rest
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


class Ed25519PolicyAuthenticator:
    """Asymmetric (Ed25519) policy authenticator.

    Can be constructed in two modes:
      - Signer mode: pass private_key_pem (can both sign and verify).
      - Verifier mode: pass public_key_pem (can only verify; sign() raises).

    The authenticator stores raw PEM bytes (picklable). Key objects are
    reconstructed lazily from the PEM on each sign/verify call (cheap
    for Ed25519).

    Usage (signer):
        priv_pem, pub_pem = generate_ed25519_keypair()
        signer = Ed25519PolicyAuthenticator(private_key_pem=priv_pem,
                                              signer_id="policy-authority")
        signed = sign_policy(policy, signer)

    Usage (verifier):
        verifier = Ed25519PolicyAuthenticator(public_key_pem=pub_pem,
                                                signer_id="policy-authority")
        assert signed.verify(verifier) is True

    Trust model:
      - The public key is meant to be distributed to all verifiers.
        Anyone holding the public key can verify; only the private-key
        holder can sign.
      - UPMP does not implement PKI or key distribution. The caller is
        responsible for ensuring the public key is authentic (e.g., via
        a trusted key directory, a certificate chain, or a pinned key).
      - The signer_id field is informational; UPMP does not verify that
        it maps to the key. Callers should verify the key fingerprint
        matches the expected signer_id out-of-band.
    """

    def __init__(self,
                 private_key_pem: Optional[bytes] = None,
                 public_key_pem: Optional[bytes] = None,
                 signer_id: str = "ed25519") -> None:
        _require_cryptography()
        if private_key_pem is None and public_key_pem is None:
            raise ValueError("must provide private_key_pem or public_key_pem")
        if not signer_id:
            raise ValueError("signer_id must be non-empty")
        self._private_key_pem = private_key_pem
        self._public_key_pem = public_key_pem
        self.signer_id = signer_id
        # If only private key given, derive the public key for verification.
        if private_key_pem is not None and public_key_pem is None:
            priv = load_pem_private_key(private_key_pem, password=None)
            pub = priv.public_key()
            self._public_key_pem = pub.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )

    def _load_private_key(self) -> Ed25519PrivateKey:
        if self._private_key_pem is None:
            raise TypeError("this authenticator has no private key (verifier-only); cannot sign")
        return load_pem_private_key(self._private_key_pem, password=None)

    def _load_public_key(self) -> Ed25519PublicKey:
        return load_pem_public_key(self._public_key_pem)

    def sign(self, policy: VerificationPolicy) -> str:
        """Produce an Ed25519 signature over canonical_policy(policy).

        Returns the signature as a hex string.

        Raises:
            TypeError: if this authenticator was constructed with only
                a public key (verifier-only mode).
        """
        priv = self._load_private_key()
        payload = canonical_policy(policy).encode("utf-8")
        sig = priv.sign(payload)
        return sig.hex()

    def verify(self, policy: VerificationPolicy, signature: str) -> bool:
        """Verify an Ed25519 signature. Returns True/False; never raises."""
        if not isinstance(signature, str):
            return False
        try:
            sig_bytes = bytes.fromhex(signature)
        except ValueError:
            return False  # not valid hex
        try:
            pub = self._load_public_key()
            payload = canonical_policy(policy).encode("utf-8")
            pub.verify(sig_bytes, payload)
            return True
        except InvalidSignature:
            return False
        except Exception:
            # Any other error (malformed policy, etc.) -> not verified.
            return False

    @property
    def is_signer(self) -> bool:
        """True iff this authenticator can sign (has a private key)."""
        return self._private_key_pem is not None

    @property
    def public_key_pem(self) -> bytes:
        """The public key in PEM format (always available — derived from
        the private key if only the private key was provided)."""
        return self._public_key_pem
