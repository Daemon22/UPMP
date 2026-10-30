"""Cooperative cancellation primitives for UPMP workflow execution.

M2 scope — provides the smallest safe first step toward cancellation
and timeout support in the closed-loop orchestrator. The design is
COOPERATIVE: handlers receive a CancellationToken via ActionRequest and
are expected to poll it periodically. A handler that ignores the token
and never returns cannot be preempted by this mechanism; true
preemption requires thread/process isolation (deferred to M2.2).

Design:
  - CancellationToken is the read-side handle. It exposes is_cancelled()
    and the `cancelled` property. It is safe to share across threads
    (the underlying flag is a threading.Event), but UPMP's audit sinks
    remain explicitly single-writer; cancellation does not change that.
  - CancellationTokenSource owns the write side. It creates a token and
    exposes cancel() and cancel_after(seconds). Decoupling the read and
    write sides prevents a handler from cancelling a run it did not
    initiate — only the caller that holds the source can cancel.
  - A default "never-cancelled" token (NEVER_CANCELLED) is used when no
    cancellation is requested, preserving backward compatibility for
    handlers that do not check the token.

Threat model notes (see THREAT.md):
  - Cancellation is a liveness/control feature, not a security control.
    It does not authenticate the canceller; the caller that constructs
    the orchestrator is trusted to decide when to cancel.
  - A cancelled run produces a WorkflowResult with status=CANCELLED and
    a complete audit trail up to the cancellation point. The audit
    chain remains verifiable (events already recorded are not retracted).
  - Timeout (per-step or run-level deadline) is a separate signal that
    transitions the run to TIMEOUT or routes via on_indeterminate; it
    does not preempt the handler either.
"""
from __future__ import annotations

import threading
from typing import Optional


class CancellationToken:
    """Read-side handle for cooperative cancellation.

    Created by CancellationTokenSource. Handlers poll is_cancelled()
    periodically and exit early (returning ActionStatus.INDETERMINATE
    or raising) when the token is cancelled.

    Thread-safety: the underlying flag is a threading.Event, so
    is_cancelled() is safe to call from any thread. Cancellation is a
    one-way transition — once cancelled, a token cannot be un-cancelled.
    """

    __slots__ = ("_event",)

    def __init__(self, event: Optional[threading.Event] = None) -> None:
        self._event = event if event is not None else threading.Event()

    def is_cancelled(self) -> bool:
        """Return True if cancel() has been called on the source."""
        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        """Property alias for is_cancelled(). Read-only."""
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until cancelled or timeout seconds elapse.

        Returns True if cancelled, False if the timeout elapsed first.
        Useful for handlers that need to sleep but want to wake early on
        cancellation.
        """
        return self._event.wait(timeout)

    def __repr__(self) -> str:
        return "CancellationToken(cancelled=%s)" % self._event.is_set()


class CancellationTokenSource:
    """Write-side factory for CancellationToken.

    The source owns the cancel() method; the token it produces is
    read-only. This decoupling prevents a handler from cancelling a run
    it did not initiate — only the caller that holds the source can
    cancel.

    Usage:
        source = CancellationTokenSource()
        orchestrator = ClosedLoopOrchestrator(
            steps, dispatcher, cancellation_token=source.token)
        # ... start run in another thread ...
        source.cancel()  # request cancellation
    """

    __slots__ = ("_event", "token")

    def __init__(self) -> None:
        self._event = threading.Event()
        self.token = CancellationToken(self._event)

    def cancel(self) -> None:
        """Request cancellation. One-way transition; idempotent."""
        self._event.set()

    def cancel_after(self, seconds: float) -> "threading.Timer":
        """Schedule cancellation after `seconds` seconds.

        Returns the Timer (started) so the caller can cancel the
        pending timer if needed via .cancel(). The timer fires in a
        background daemon thread.
        """
        timer = threading.Timer(seconds, self._event.set)
        timer.daemon = True
        timer.start()
        return timer

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def __repr__(self) -> str:
        return "CancellationTokenSource(cancelled=%s)" % self._event.is_set()


# Module-level default token for backward compatibility: handlers that
# do not receive an explicit token see a never-cancelled token, so
# existing code paths that never check cancellation behave identically
# to pre-M2 UPMP.
NEVER_CANCELLED = CancellationToken()
