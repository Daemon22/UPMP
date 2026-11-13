"""Read-only local HTTP bridge for a River Map snapshot and its visual UI."""
from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit


def make_observatory_server(
    snapshot_path: str,
    ui_root: str,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Serve a static River Map UI and a read-only JSON process snapshot.

    The listener defaults to loopback. The endpoint serves only the specified
    snapshot file and only accepts GET requests; applications that expose a
    remote dashboard should put their own authentication and TLS boundary in
    front of this development bridge.
    """
    snapshot = Path(snapshot_path).resolve()
    root = Path(ui_root).resolve()
    if not snapshot.is_file():
        raise FileNotFoundError("process snapshot does not exist: %s" % snapshot)
    if not root.is_dir():
        raise NotADirectoryError("UI root is not a directory: %s" % root)

    class Handler(SimpleHTTPRequestHandler):
        snapshot_path = snapshot
        ui_root = root

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if urlsplit(self.path).path == "/api/process-run":
                try:
                    payload = json.loads(snapshot.read_text(encoding="utf-8"))
                    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    self.send_error(503, "process snapshot is not readable")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)
                return
            if urlsplit(self.path).path == "/":
                self.path = "/live.html"
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            self.send_error(405, "read-only observatory")

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep the development console legible; override by replacing the
            # handler if an application needs standard access logging.
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve_observatory(
    snapshot_path: str,
    ui_root: str,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Run the read-only local snapshot bridge until interrupted."""
    server = make_observatory_server(snapshot_path, ui_root, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
