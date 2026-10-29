"""Policy signing and version lifecycle for UPMP verification policies.

M4 scope — provides authenticated integrity for VerificationPolicy
objects. A signed policy carries a cryptographic digest (HMAC-SHA256
by default) over its canonical serialization, so that a tampered policy
cannot pass verification undetected.

THREAT.md T-NEW addressed: "VerificationPolicy cannot be authenticated;
a tampered policy could pass anything." With M4, a policy that has been
modified after signing will fail signature verification, and the
orchestrator (or any caller) can refuse to use unsigned or
badly-signed policies.

Design:
  - PolicyAuthenticator is a Protocol with sign(policy) -> str and
    verify(policy, signature) -> bool. Implementations choose the
    signing primitive (HMAC, asymmetric, external KMS, etc.).
  - HmacPolicyAuthenticator is the default stdlib implementation using
    hmac + hashlib.sha256 with a caller-provided shared secret. The
    secret is NEVER stored on the policy; only the signature is.
  - SignedPolicy is a frozen dataclass wrapping a VerificationPolicy
    plus its signature and signer_id. It carries the policy and its
    authentication together.
  - canonical_policy(policy) produces a deterministic JSON serialization
    of the policy (policy_id, version, sorted constraints) for signing.

Threat model notes (see THREAT.md):
  - The signature authenticates the policy's CONTENTS, not its ORIGIN.
    A signer_id field records who signed, but UPMP does not implement
    PKI or key management — the caller is responsible for verifying
    that signer_id maps to a trusted key.
  - HMAC with a shared secret provides authentication between parties
    that share the secret. For multi-party authentication, use
    asymmetric signing (deferred to M4.1).
  - The signature does NOT prevent rollback to an older validly-signed
    policy. Callers should track the latest known version per policy_id
    and refuse older versions (version lifecycle is the caller's
    responsibility; M4 provides the signing primitive only).
  - The secret is held in process memory. An attacker with process
    memory access can forge signatures. Protect the secret with OS-
    level controls (key management service, secrets vault) in
    production.

Backward compatibility:
  - VerificationPolicy is unchanged. Signing is opt-in via the
    PolicyAuthenticator Protocol and SignedPolicy wrapper. Existing
    code that constructs VerificationPolicy directly continues to work
    without signing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol, Sequence

from .contracts import Constraint, VerificationPolicy


def canonical_policy(policy: VerificationPolicy) -> str:
    """Deterministic JSON serialization of a VerificationPolicy.

    Used for signing — the signature covers this exact byte sequence.
    Constraints are serialized in their declaration order (the policy's
    author controls the order); within each constraint, keys are sorted
    for determinism. Non-serializable values are coerced via default=str.
    """
    constraints = []
    for c in policy.constraints:
        # Constraint fields (per UPMP/contracts.py): field, operator,
        # expected, unit, max_age_seconds, tolerance, name, description.
        # Only serialize non-None fields (None means "not specified" and
        # should not affect the signature).
        record = {}
        for f_name in ("field", "operator", "expected", "unit",
                        "max_age_seconds", "tolerance", "name", "description"):
            val = getattr(c, f_name, None)
            if val is not None:
                record[f_name] = val
        constraints.append(record)
    payload = {
        "policy_id": policy.policy_id,
        "version": policy.version,
        "constraints": constraints,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class PolicyAuthenticator(Protocol):
    """Protocol for signing and verifying VerificationPolicy objects.

    Implementations MUST:
      - sign(policy) -> str: return a cryptographic digest over
        canonical_policy(policy). The digest must be deterministic
        (same policy + same key -> same digest).
      - verify(policy, signature) -> bool: return True iff the
        signature is valid for the policy under the authenticator's
        key(s). Must NOT raise on bad input; return False instead.

    Implementations MAY:
      - Support multiple signers (identified by signer_id).
      - Delegate to an external KMS or HSM.
    """

    def sign(self, policy: VerificationPolicy) -> str:
        """Produce a signature over canonical_policy(policy)."""
        ...

    def verify(self, policy: VerificationPolicy, signature: str) -> bool:
        """Verify a signature against canonical_policy(policy)."""
        ...


class HmacPolicyAuthenticator:
    """Default HMAC-SHA256 policy authenticator.

    Uses a caller-provided shared secret. The secret is held in
    process memory and is NEVER stored on the policy. Multiple
    authenticators sharing the same secret can verify each other's
    signatures.

    For multi-party or asymmetric authentication, provide a custom
    PolicyAuthenticator implementation (e.g. Ed25519 via cryptography
    library — deferred to M4.1).
    """

    def __init__(self, secret: bytes, signer_id: str = "default") -> None:
        if not secret:
            raise ValueError("secret must be non-empty")
        if not signer_id:
            raise ValueError("signer_id must be non-empty")
        self._secret = secret if isinstance(secret, bytes) else secret.encode("utf-8")
        self.signer_id = signer_id

    def sign(self, policy: VerificationPolicy) -> str:
        """Produce an HMAC-SHA256 digest over canonical_policy(policy)."""
        payload = canonical_policy(policy).encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def verify(self, policy: VerificationPolicy, signature: str) -> bool:
        """Verify an HMAC-SHA256 signature. Returns False on any
        mismatch or malformed input; never raises."""
        if not isinstance(signature, str):
            return False
        try:
            expected = self.sign(policy)
        except Exception:
            return False
        return hmac.compare_digest(expected, signature)


@dataclass(frozen=True)
class SignedPolicy:
    """A VerificationPolicy paired with its cryptographic signature.

    The signature covers canonical_policy(policy) under the
    signer's key. Verify with PolicyAuthenticator.verify(policy, signature).

    The signer_id records who signed; UPMP does not verify that
    signer_id maps to a trusted key (the caller is responsible for
    key management).
    """
    policy: VerificationPolicy
    signature: str
    signer_id: str = "default"
    signed_at: str = field(default_factory=lambda: __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat())

    def verify(self, authenticator: PolicyAuthenticator) -> bool:
        """Verify this policy's signature under the given authenticator.

        Returns True iff the signature is valid AND the signer_id
        matches the authenticator's signer_id (when the authenticator
        has one). For custom authenticators without a signer_id
        attribute, the signer_id check is skipped.
        """
        # Check signer_id match for any authenticator that has one
        # (HmacPolicyAuthenticator M4, Ed25519PolicyAuthenticator M4.1,
        # and any custom authenticator exposing signer_id).
        auth_signer_id = getattr(authenticator, "signer_id", None)
        if auth_signer_id is not None and self.signer_id != auth_signer_id:
            return False
        return authenticator.verify(self.policy, self.signature)


def sign_policy(policy: VerificationPolicy,
                authenticator: PolicyAuthenticator,
                signer_id: Optional[str] = None) -> SignedPolicy:
    """Convenience: sign a policy with an authenticator, returning a SignedPolicy.

    The signer_id is taken from the explicit argument, or from the
    authenticator's signer_id attribute (if it has one), or defaults to
    "default". Works with both HmacPolicyAuthenticator (M4) and
    Ed25519PolicyAuthenticator (M4.1)."""
    sid = signer_id
    if sid is None:
        sid = getattr(authenticator, "signer_id", None) or "default"
    sig = authenticator.sign(policy)
    return SignedPolicy(policy=policy, signature=sig, signer_id=sid)
