"""Preemptible dispatcher for UPMP — thread-based handler preemption.

M2.2 scope — provides preemption for handlers that ignore the
cooperative CancellationToken (M2) and run past their timeout. This
closes the M2 limitation: "a handler that ignores the token and never
returns cannot be preempted."

Design:
  - PreemptibleDispatcher wraps any ActionDispatcher (or compatible
    object with dispatch/ register/ allowed_actions/ authorize/
    idempotency_store attributes).
  - When default_timeout_seconds is set, every dispatch() runs the
    wrapped dispatcher's dispatch in a daemon worker thread, joined
    with the timeout. If the worker doesn't return in time, the
    wrapper returns ActionResult(INDETERMINATE, "preempted...") and
    leaves the worker running.
  - The wrapper also exposes register() and forwards attribute access
    so it's a near-drop-in replacement for ActionDispatcher in the
    orchestrator constructor.

Documented limitation (cannot be fixed within CPython):
  - CPython threads cannot be killed. A preempted handler keeps
    running in its daemon thread until it returns or the process
    exits. The dispatcher returns INDETERMINATE immediately; the
    orphan thread cannot affect the run's result, but it may continue
    consuming CPU/memory/IO.
  - For true preemption (handler actually stops), use process
    isolation: run the handler in a subprocess that can be SIGKILL'd.
    That is a larger change deferred to M2.3.
  - The daemon thread holds no references to the orchestrator's
    mutable state (the dispatcher is stateless across calls; the
    state and request are immutable/frozen dataclasses). So an orphan
    thread cannot corrupt the run — only waste resources.

Thread-safety of the wrapper itself:
  - Each dispatch() call spawns a fresh thread. The wrapper does not
    share mutable state across calls. The wrapped dispatcher's
    thread-safety is its own concern (ActionDispatcher is thread-safe
    for read-only operations but its idempotency_store is not).
  - Concurrent dispatch calls (e.g. from multiple orchestrator runs
    sharing a dispatcher) will each get their own worker thread.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from .closed_loop import ActionDispatcher, ActionRequest, ActionResult, ActionStatus, ProcessState


class PreemptibleDispatcher:
    """Wraps an ActionDispatcher with thread-based handler preemption.

    When default_timeout_seconds is set, every dispatch() spawns a
    daemon worker thread that runs the wrapped dispatcher's dispatch.
    The wrapper waits up to default_timeout_seconds for the worker to
    finish; if it doesn't, the wrapper returns INDETERMINATE and the
    worker is left running (Python cannot kill threads).

    Use this when handlers may ignore the cooperative
    CancellationToken (M2) and you need a hard cap on dispatch time.

    Usage:
        base = ActionDispatcher({"act"}, ...)
        base.register("act", handler)
        preemptible = PreemptibleDispatcher(base, default_timeout_seconds=0.5)
        orchestrator = ClosedLoopOrchestrator(steps, preemptible, ...)

    The wrapper forwards register() and the standard dispatcher
    attributes (allowed_actions, authorize, idempotency_store) so the
    orchestrator can treat it like an ActionDispatcher.
    """

    def __init__(self, dispatcher: ActionDispatcher,
                 default_timeout_seconds: Optional[float] = None) -> None:
        if default_timeout_seconds is not None and default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive or None")
        self._dispatcher = dispatcher
        self.default_timeout_seconds = default_timeout_seconds

    # ---- forwarded API ----

    def register(self, name: str, handler: Any) -> None:
        """Register a handler on the wrapped dispatcher."""
        self._dispatcher.register(name, handler)

    @property
    def allowed_actions(self):
        return self._dispatcher.allowed_actions

    @property
    def authorize(self):
        return self._dispatcher.authorize

    @property
    def idempotency_store(self):
        return self._dispatcher.idempotency_store

    @property
    def wrapped(self) -> ActionDispatcher:
        """The underlying dispatcher (for direct access if needed)."""
        return self._dispatcher

    # ---- preemption logic ----

    def dispatch(self, request: ActionRequest, state: ProcessState,
                 timeout_seconds: Optional[float] = None) -> ActionResult:
        """Dispatch with optional preemption.

        Args:
            request: the action request. If request.cancellation_token
                is already cancelled, returns INDETERMINATE immediately
                without spawning a worker.
            state: the process state.
            timeout_seconds: per-call override. If None, uses
                default_timeout_seconds. If both are None, dispatches
                synchronously (no preemption — backward compat).

        Returns:
            The handler's ActionResult, or INDETERMINATE if the
            handler didn't return within the timeout.
        """
        # Pre-check: if the cancellation token is already set, don't
        # even spawn a worker. This is the M2 cooperative check, applied
        # at the dispatcher layer for callers that didn't get the
        # orchestrator-level check.
        if request.cancellation_token.is_cancelled():
            return ActionResult(ActionStatus.INDETERMINATE,
                                "preempted: cancellation token already set")

        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds

        # No timeout -> dispatch synchronously (backward compat).
        if timeout is None:
            return self._dispatcher.dispatch(request, state)

        # Spawn a daemon worker and wait with timeout.
        result_box = [None]
        exc_box = [None]

        def worker():
            try:
                result_box[0] = self._dispatcher.dispatch(request, state)
            except BaseException as exc:
                # Shouldn't happen — ActionDispatcher.dispatch catches
                # handler exceptions. But just in case:
                exc_box[0] = exc

        thread = threading.Thread(target=worker, daemon=True,
                                  name="upmp-preemptible-%s" % request.action)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            # Preempted: the worker is still running. We return
            # INDETERMINATE immediately; the daemon thread will keep
            # running until it returns or the process exits.
            return ActionResult(
                ActionStatus.INDETERMINATE,
                "preempted after %.3fs (handler still running in background)" % timeout,
            )
        if exc_box[0] is not None:
            return ActionResult(
                ActionStatus.FAILED,
                "dispatch worker raised %s: %s" % (type(exc_box[0]).__name__, exc_box[0]),
            )
        return result_box[0]
