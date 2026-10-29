"""Process-level preemption for UPMP — true handler termination via SIGKILL.

M2.3 scope — provides preemption that actually TERMINATES the handler,
unlike M2.2's thread-based preemption which leaves orphaned daemon
threads running. Each dispatch() runs the handler in a child process;
if the timeout expires, the parent sends SIGTERM (then SIGKILL if
needed) to the child, guaranteeing the handler is dead before the
dispatcher returns.

Trade-off vs M2.2 (PreemptibleDispatcher, thread-based):
  + The handler is actually killed — no orphaned threads consuming
    resources after preemption.
  + A hung handler cannot affect subsequent dispatches (each gets a
    fresh process).
  - The handler MUST be a picklable top-level function. Lambdas and
    closures are NOT picklable and will raise at dispatch time.
    (On fork-start-method platforms, this is relaxed — but on Windows
    / spawn, the handler must be importable by the child process.)
  - ProcessState and ActionResult must be picklable. They are frozen
    dataclasses; values (Observation.value) must also be picklable.
  - Subprocess overhead: ~5-50ms per dispatch (process spawn), vs
    ~0.05ms for a thread. Not suitable for high-throughput use.
  - The child process inherits the parent's memory at fork time; large
    parent state can make spawning expensive.

Threat model notes (see THREAT.md):
  - Process isolation means a handler that crashes (segfault, OOM,
    infinite loop) cannot corrupt the parent's state. The dispatcher
    returns FAILED or INDETERMINATE and the parent continues.
  - The child process does NOT share the parent's audit sink, audit
    events, or in-memory state. Only the dispatch result is sent back
    via the Queue. This is by design — process isolation is the point.
  - On non-fork platforms (Windows spawn), the child re-imports the
    module containing the handler. Module-level side effects run twice.
    Keep module top-level code side-effect-free.

Backward compatibility:
  - Pure addition. PreemptibleDispatcher (M2.2) is unchanged. Callers
    choose between thread-based (any callable, soft preemption) and
    process-based (picklable handlers only, hard preemption).
"""
from __future__ import annotations

import multiprocessing
import os
import signal
import time
from typing import Any, Optional, Union
from queue import Empty as QueueEmpty

from .closed_loop import ActionDispatcher, ActionRequest, ActionResult, ActionStatus, ProcessState


# Module-level worker function. Must be top-level (not a closure) so
# it is picklable for spawn start method. The wrapped dispatcher and
# the request/state are passed as arguments (they must also be picklable).
def _process_worker(dispatcher: ActionDispatcher,
                    request: ActionRequest,
                    state: ProcessState,
                    result_queue: multiprocessing.Queue) -> None:
    """Run the dispatcher's dispatch in a child process. Puts the
    result (or the exception) on the queue. Does NOT catch exceptions
    from the dispatcher itself — ActionDispatcher.dispatch already
    catches handler exceptions and returns FAILED. We only catch
    pickling errors on the queue.put."""
    try:
        result = dispatcher.dispatch(request, state)
        result_queue.put(("result", result))
    except BaseException as exc:
        # Shouldn't happen (ActionDispatcher.dispatch catches handler
        # exceptions), but just in case — send the exception back so
        # the parent can return FAILED instead of hanging.
        try:
            result_queue.put(("exception", type(exc).__name__, str(exc)))
        except Exception:
            # Last resort: signal that something went wrong.
            result_queue.put(("error", "unpicklable exception"))


class ProcessPreemptibleDispatcher:
    """Wraps an ActionDispatcher with process-based handler preemption.

    Each dispatch() spawns a fresh child process that runs the wrapped
    dispatcher's dispatch. The parent waits up to default_timeout_seconds
    for the result; if the timeout expires, the parent terminates the
    child (SIGTERM, then SIGKILL if needed) and returns INDETERMINATE.

    Unlike PreemptibleDispatcher (M2.2, thread-based), the handler is
    actually KILLED — no orphaned threads. The trade-off is that the
    handler must be a picklable top-level function (no lambdas or
    closures), and there is subprocess overhead per dispatch.

    Usage:
        base = ActionDispatcher({"act"})
        base.register("act", my_top_level_handler)  # NOT a lambda
        preemptible = ProcessPreemptibleDispatcher(base, default_timeout_seconds=0.5)
        orchestrator = ClosedLoopOrchestrator(steps, preemptible, ...)

    The wrapper forwards register() and standard dispatcher attributes
    so the orchestrator can treat it like an ActionDispatcher.
    """

    def __init__(self, dispatcher: ActionDispatcher,
                 default_timeout_seconds: Optional[float] = None,
                 kill_grace_seconds: float = 0.5) -> None:
        if default_timeout_seconds is not None and default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive or None")
        if kill_grace_seconds < 0:
            raise ValueError("kill_grace_seconds cannot be negative")
        self._dispatcher = dispatcher
        self.default_timeout_seconds = default_timeout_seconds
        self.kill_grace_seconds = kill_grace_seconds

    # ---- forwarded API ----

    def register(self, name: str, handler: Any) -> None:
        """Register a handler on the wrapped dispatcher.

        The handler MUST be a picklable top-level function for
        process-based dispatch to work. Lambdas and closures will
        raise PicklingError at dispatch time."""
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

    # ---- process-based preemption logic ----

    def dispatch(self, request: ActionRequest, state: ProcessState,
                 timeout_seconds: Optional[float] = None) -> ActionResult:
        """Dispatch with process-based preemption.

        Args:
            request: the action request. If request.cancellation_token
                is already cancelled, returns INDETERMINATE immediately
                without spawning a process.
            state: the process state (must be picklable).
            timeout_seconds: per-call override. If None, uses
                default_timeout_seconds. If both are None, dispatches
                synchronously (no preemption — backward compat).

        Returns:
            The handler's ActionResult, or INDETERMINATE if the handler
            didn't return within the timeout (and was then killed).
        """
        # Pre-check: if the cancellation token is already set, don't
        # spawn a process.
        if request.cancellation_token.is_cancelled():
            return ActionResult(ActionStatus.INDETERMINATE,
                                "preempted: cancellation token already set")

        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds

        # No timeout -> dispatch synchronously (backward compat).
        # This avoids subprocess overhead when preemption isn't needed.
        if timeout is None:
            return self._dispatcher.dispatch(request, state)

        # Spawn a child process and wait with timeout.
        ctx = multiprocessing.get_context("fork") if hasattr(os, "fork") else multiprocessing.get_context()
        result_queue = ctx.Queue()

        try:
            process = ctx.Process(
                target=_process_worker,
                args=(self._dispatcher, request, state, result_queue),
                daemon=True,
                name="upmp-process-%s" % request.action,
            )
        except Exception as exc:
            # Pickling the arguments failed (e.g., handler is a lambda).
            return ActionResult(
                ActionStatus.FAILED,
                "could not spawn worker process: %s: %s (handler must be a picklable top-level function)"
                % (type(exc).__name__, exc),
            )

        process.start()
        try:
            kind, *rest = result_queue.get(timeout=timeout)
        except QueueEmpty:
            # Timeout — kill the child.
            self._kill_process(process)
            return ActionResult(
                ActionStatus.INDETERMINATE,
                "preempted after %.3fs (handler process killed)" % timeout,
            )

        # Wait for the process to exit cleanly.
        process.join(timeout=self.kill_grace_seconds)
        if process.is_alive():
            # The worker put the result but didn't exit — force kill.
            self._kill_process(process)

        if kind == "result":
            return rest[0]
        elif kind == "exception":
            return ActionResult(
                ActionStatus.FAILED,
                "dispatch worker raised %s: %s" % (rest[0], rest[1]),
            )
        else:
            return ActionResult(
                ActionStatus.FAILED,
                "dispatch worker reported an unpicklable error",
            )

    def _kill_process(self, process: multiprocessing.Process) -> None:
        """Terminate the child process: SIGTERM, wait, SIGKILL if still alive."""
        if not process.is_alive():
            return
        process.terminate()  # SIGTERM
        process.join(timeout=self.kill_grace_seconds)
        if process.is_alive():
            try:
                # SIGKILL — the process cannot catch this.
                if hasattr(process, "kill"):
                    process.kill()
                else:
                    os.kill(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.join(timeout=self.kill_grace_seconds)
