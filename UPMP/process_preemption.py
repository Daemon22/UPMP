"""Process-level preemption with import-resolved handlers.

Timed dispatches run in a small, reusable worker process. The worker receives
only a handler import reference, the action and picklable request fields, and
the process state. Authorization, idempotency, cancellation, and timeout
enforcement remain in the parent process.
"""
from __future__ import annotations

import atexit
import importlib
import inspect
import os
import pickle
import subprocess
import sys
import threading
import time
from typing import Any, Optional

from .closed_loop import ActionDispatcher, ActionRequest, ActionResult, ActionStatus, ProcessState


_FRAME_SIZE_BYTES = 8
_WORKER_READY = b"R"
_WORKER_PROCESSES = set()
_WORKER_PROCESSES_LOCK = threading.Lock()


def _resolve_handler(handler_reference: str) -> Any:
    """Import ``module:qualified_name`` and return its callable target."""
    module_name, separator, qualified_name = handler_reference.partition(":")
    if not separator or not module_name or not qualified_name:
        raise ImportError("invalid handler import reference")
    module = importlib.import_module(module_name)
    handler = module
    for attribute in qualified_name.split("."):
        handler = getattr(handler, attribute)
    if not callable(handler):
        raise TypeError("imported handler is not callable")
    return handler


def _read_exact(stream: Any, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("worker pipe closed before the message was complete")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_frame(stream: Any, value: Any) -> None:
    payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(len(payload).to_bytes(_FRAME_SIZE_BYTES, "big"))
    stream.write(payload)
    stream.flush()


def _read_frame(stream: Any) -> Any:
    size = int.from_bytes(_read_exact(stream, _FRAME_SIZE_BYTES), "big")
    return pickle.loads(_read_exact(stream, size))


def _subprocess_worker() -> None:
    """Serve request frames until the parent closes the input pipe."""
    input_stream = sys.stdin.buffer
    output_stream = sys.stdout.buffer
    # User handlers may print; keep their output away from the framed IPC.
    devnull = open(os.devnull, "w")
    sys.stdout = devnull
    sys.stderr = devnull
    output_stream.write(_WORKER_READY)
    output_stream.flush()

    while True:
        header = input_stream.read(_FRAME_SIZE_BYTES)
        if not header:
            break
        try:
            size = int.from_bytes(header + _read_exact(input_stream, _FRAME_SIZE_BYTES - len(header)), "big")
            message = pickle.loads(_read_exact(input_stream, size))
            handler = _resolve_handler(message["handler_reference"])
            request_fields = message["request_fields"]
            request = ActionRequest(
                action=message["action"],
                parameters=request_fields["parameters"],
                principal_ref=request_fields["principal_ref"],
                request_id=request_fields["request_id"],
                idempotency_key=request_fields["idempotency_key"],
            )
            try:
                result = handler(request, message["state"])
                if not isinstance(result, ActionResult):
                    result = ActionResult(
                        ActionStatus.INDETERMINATE,
                        "handler returned an invalid result",
                    )
                response = ("result", result)
            except Exception as exc:
                response = (
                    "error",
                    "action handler raised %s: %s" % (type(exc).__name__, exc),
                )
        except Exception as exc:
            response = (
                "error",
                "child could not import or run handler: %s: %s"
                % (type(exc).__name__, exc),
            )

        try:
            _write_frame(output_stream, response)
        except Exception as exc:
            # A result may contain an unpicklable object. Send a plain-text
            # failure instead so the parent never mistakes it for a hang.
            try:
                _write_frame(
                    output_stream,
                    ("error", "handler result could not be serialized: %s: %s"
                     % (type(exc).__name__, exc)),
                )
            except Exception:
                break
    devnull.close()


def _cleanup_workers() -> None:
    with _WORKER_PROCESSES_LOCK:
        workers = list(_WORKER_PROCESSES)
        _WORKER_PROCESSES.clear()
    for process in workers:
        _stop_process(process, 0.2)


def _stop_process(process: subprocess.Popen, grace_seconds: float) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=grace_seconds or 0.1)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    with _WORKER_PROCESSES_LOCK:
        _WORKER_PROCESSES.discard(process)


atexit.register(_cleanup_workers)


class ProcessPreemptibleDispatcher:
    """Run registered module-level handlers in a killable worker process.

    The parent keeps authorization, idempotency, cancellation and timeout
    control. Timed handlers must be importable functions defined at module
    scope. Lambdas, closures and bound methods return FAILED without running.
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
        self._worker: Optional[subprocess.Popen] = None
        self._worker_error: Optional[str] = None
        self._worker_lock = threading.RLock()
        if default_timeout_seconds is not None:
            self._start_worker()

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

    def _cache_result(self, request: ActionRequest, result: ActionResult) -> ActionResult:
        store = self.idempotency_store
        if store is not None and request.idempotency_key:
            store.put(request.action, request.idempotency_key, result)
        return result

    @staticmethod
    def _handler_reference(handler: Any) -> str:
        if not inspect.isfunction(handler):
            raise TypeError("handler must be an importable module-level function")
        module_name = getattr(handler, "__module__", "")
        qualified_name = getattr(handler, "__qualname__", "")
        if (not module_name or module_name in ("__main__", "__mp_main__")
                or not qualified_name or "<locals>" in qualified_name
                or "<lambda>" in qualified_name):
            raise TypeError("handler must be an importable module-level function")
        return "%s:%s" % (module_name, qualified_name)

    def _start_worker(self) -> bool:
        if self._worker is not None and self._worker.poll() is None:
            return True
        self._worker = None
        self._worker_error = None
        package_path = os.path.dirname(os.path.abspath(__file__))
        package_parent = os.path.dirname(package_path)
        environment = os.environ.copy()
        search_path = list(dict.fromkeys(path for path in sys.path if path))
        inherited_path = environment.get("PYTHONPATH")
        if inherited_path:
            search_path.extend(path for path in inherited_path.split(os.pathsep)
                               if path and path not in search_path)
        if search_path:
            environment["PYTHONPATH"] = os.pathsep.join(search_path)
        bootstrap = (
            "import sys,types;"
            "root=sys.argv[1];"
            "package=types.ModuleType('UPMP');"
            "package.__path__=[root];"
            "sys.modules['UPMP']=package;"
            "from UPMP.process_preemption import _subprocess_worker;"
            "_subprocess_worker()"
        )
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", bootstrap, package_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                cwd=package_parent,
                env=environment,
            )
            ready = []
            ready_event = threading.Event()

            def read_ready() -> None:
                try:
                    ready.append(process.stdout.read(1))
                except (OSError, ValueError):
                    ready.append(b"")
                finally:
                    ready_event.set()

            threading.Thread(target=read_ready, daemon=True).start()
            if not ready_event.wait(30) or ready != [_WORKER_READY]:
                _stop_process(process, self.kill_grace_seconds)
                self._worker_error = "worker did not initialize or could not import UPMP"
                return False
            self._worker = process
            with _WORKER_PROCESSES_LOCK:
                _WORKER_PROCESSES.add(process)
            return True
        except (OSError, ValueError) as exc:
            self._worker_error = "could not start worker process: %s: %s" % (
                type(exc).__name__, exc,
            )
            return False

    def _discard_worker(self) -> None:
        process = self._worker
        self._worker = None
        if process is not None:
            _stop_process(process, self.kill_grace_seconds)

    def close(self) -> None:
        """Stop and reap this dispatcher's worker process, if any."""
        with self._worker_lock:
            self._discard_worker()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def dispatch(self, request: ActionRequest, state: ProcessState,
                 timeout_seconds: Optional[float] = None) -> ActionResult:
        """Dispatch an action, enforcing timeout and cancellation in parent."""
        if request.cancellation_token.is_cancelled():
            return ActionResult(
                ActionStatus.INDETERMINATE,
                "preempted: cancellation token already set",
            )

        timeout = (timeout_seconds if timeout_seconds is not None
                   else self.default_timeout_seconds)
        if timeout is None:
            return self._dispatcher.dispatch(request, state)

        if (request.action not in self._dispatcher.allowed_actions
                or request.action not in self._dispatcher._handlers):
            return ActionResult(ActionStatus.DENIED, "action is not authorized")

        store = self.idempotency_store
        if store is not None and request.idempotency_key:
            cached = store.get(request.action, request.idempotency_key)
            if cached is not None:
                return cached

        authorize = self.authorize
        if authorize is not None:
            try:
                if not authorize(request, state):
                    return self._cache_result(
                        request,
                        ActionResult(ActionStatus.DENIED,
                                     "external authorization policy denied action"),
                    )
            except Exception:
                return self._cache_result(
                    request,
                    ActionResult(ActionStatus.DENIED,
                                 "external authorization policy unavailable"),
                )

        try:
            handler_reference = self._handler_reference(
                self._dispatcher._handlers[request.action]
            )
        except Exception as exc:
            return self._cache_result(
                request,
                ActionResult(
                    ActionStatus.FAILED,
                    "could not resolve handler for child process: %s: %s"
                    % (type(exc).__name__, exc),
                ),
            )

        request_fields = {
            "parameters": request.parameters,
            "principal_ref": request.principal_ref,
            "request_id": request.request_id,
            "idempotency_key": request.idempotency_key,
        }
        try:
            payload = pickle.dumps({
                "handler_reference": handler_reference,
                "action": request.action,
                "request_fields": request_fields,
                "state": state,
            }, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            self._discard_worker()
            return self._cache_result(
                request,
                ActionResult(
                    ActionStatus.FAILED,
                    "worker request could not be serialized: %s: %s"
                    % (type(exc).__name__, exc),
                ),
            )

        with self._worker_lock:
            if not self._start_worker():
                return self._cache_result(
                    request,
                    ActionResult(
                        ActionStatus.FAILED,
                        self._worker_error or "could not start worker process",
                    ),
                )
            process = self._worker
            try:
                assert process is not None and process.stdin is not None
                process.stdin.write(len(payload).to_bytes(_FRAME_SIZE_BYTES, "big"))
                process.stdin.write(payload)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                self._discard_worker()
                return self._cache_result(
                    request,
                    ActionResult(
                        ActionStatus.FAILED,
                        "could not send request to worker process: %s: %s"
                        % (type(exc).__name__, exc),
                    ),
                )

            response = []
            response_event = threading.Event()

            def read_response() -> None:
                try:
                    assert process is not None and process.stdout is not None
                    response.append(_read_frame(process.stdout))
                except Exception as exc:
                    response.append(("error", "worker response could not be read: %s: %s"
                                     % (type(exc).__name__, exc)))
                finally:
                    response_event.set()

            threading.Thread(target=read_response, daemon=True).start()
            deadline = time.monotonic() + timeout
            while not response_event.is_set():
                if request.cancellation_token.is_cancelled():
                    self._discard_worker()
                    return self._cache_result(
                        request,
                        ActionResult(
                            ActionStatus.INDETERMINATE,
                            "preempted: cancellation token set; handler process killed",
                        ),
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._discard_worker()
                    return self._cache_result(
                        request,
                        ActionResult(
                            ActionStatus.INDETERMINATE,
                            "preempted after %.3fs (handler process killed)" % timeout,
                        ),
                    )
                response_event.wait(min(remaining, 0.02))

            kind, result = response[0]
            if kind == "result" and isinstance(result, ActionResult):
                return self._cache_result(request, result)
            return self._cache_result(
                request,
                ActionResult(ActionStatus.FAILED, str(result)),
            )
