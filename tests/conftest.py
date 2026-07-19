from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class RecordingServer:
    """Minimal HTTP server that records every request for assertions."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.response_status = 200
        self.get_responses: dict[str, dict] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self, body) -> None:
                server.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": body,
                    }
                )

            def _respond(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = raw.decode("utf-8", errors="replace")
                self._record(body)
                self._respond(server.response_status, {"ok": server.response_status == 200})

            def do_GET(self) -> None:
                self._record(None)
                path = self.path.split("?")[0]
                if path in server.get_responses:
                    self._respond(server.response_status, server.get_responses[path])
                else:
                    self._respond(404, {"error": "not found"})

            def log_message(self, *args) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def chat_server():
    server = RecordingServer()
    server.start()
    yield server
    server.stop()
