"""Idempotency store for UPMP action dispatch.

M3 scope — provides the smallest safe first step toward enforced
idempotency at the dispatcher layer. When an IdempotencyStore is
configured on ActionDispatcher, retries with the same idempotency_key
return the cached ActionResult instead of re-invoking the handler.

Design:
  - IdempotencyStore is a Protocol with get/put/contains/clear.
  - InMemoryIdempotencyStore is the default in-process implementation.
    It is dict-backed and unbounded. For production use, a persistent
    store (e.g. Redis, SQLite-backed) should be provided by the
    deployment.
  - The store is keyed by (action, idempotency_key). The action name
    is included in the key to prevent cross-action collisions when
    two actions happen to use the same idempotency_key string.

Backward compatibility:
  - When no store is configured (the default), ActionDispatcher
    behaves exactly as before: the handler is called on every dispatch,
    and the handler is responsible for deduplication. This preserves
    the existing contract documented in ARCHITECTURE.md and README.

Threat model notes (see THREAT.md):
  - Idempotency is a correctness property, not a security control. The
    store does not authenticate the caller; it deduplicates by key.
  - The store is NOT thread-safe by default. Concurrent dispatchers
    sharing a store must serialize externally (e.g. via a lock). The
    in-memory store uses a plain dict; concurrent puts may race.
  - A store entry is retained indefinitely in the in-memory
    implementation. Production deployments should use a store with
    TTL or LRU eviction to bound memory usage.
  - The store does NOT validate that the cached result is still valid
    for the current state. If a handler's result depends on state, the
    caller is responsible for ensuring the idempotency_key encodes
    enough context (e.g. includes a state version) to make caching safe.
"""
from __future__ import annotations

from typing import Dict, Optional, Protocol, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .closed_loop import ActionResult


class IdempotencyStore(Protocol):
    """Protocol for idempotency-cached action results.

    Implementations MUST be safe to call from a single dispatcher
    thread. For concurrent dispatchers sharing a store, the caller
    MUST serialize access externally.
    """

    def get(self, action: str, key: str) -> Optional["ActionResult"]:
        """Return the cached result for (action, key), or None if not
        present. Must NOT raise on missing keys."""
        ...

    def put(self, action: str, key: str, result: "ActionResult") -> None:
        """Cache a result for (action, key). Overwrites any existing
        entry for the same key."""
        ...

    def contains(self, action: str, key: str) -> bool:
        """Return True iff (action, key) has a cached result."""
        ...

    def clear(self) -> None:
        """Remove all cached results. Useful for testing."""
        ...


class InMemoryIdempotencyStore:
    """Default in-process idempotency store.

    Backed by a plain dict; unbounded; not thread-safe. Suitable for
    single-process workflows and testing. For production use, provide
    a persistent or shared store (Redis, SQLite, etc.).
    """

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str], "ActionResult"] = {}

    def get(self, action: str, key: str) -> Optional["ActionResult"]:
        return self._entries.get((action, key))

    def put(self, action: str, key: str, result: "ActionResult") -> None:
        self._entries[(action, key)] = result

    def contains(self, action: str, key: str) -> bool:
        return (action, key) in self._entries

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return "InMemoryIdempotencyStore(entries=%d)" % len(self)
