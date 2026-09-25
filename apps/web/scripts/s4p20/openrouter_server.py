"""OpenRouter for S4-P20's isolated stack. Nothing here reaches the live API.

The api asks OpenRouter one thing in this stack: whether the workspace's key
resolves (CR-E7) when a run starts. Every answer is S4-P8's scripted
OpenRouter (`test_s4p8_extras._Script`), the same script `seed.py` executes the
run against in-process. Runs inside the api image:
`python /app/s4p20/openrouter_server.py`.
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

sys.path.insert(0, "/app")

from tests.integration.test_s4p8_extras import _Script  # noqa: E402

PORT = 8099
SCRIPT = _Script()


class Handler(BaseHTTPRequestHandler):
    def _route(self, method: str, body: bytes) -> httpx.Response:
        return SCRIPT.fake.handler(httpx.Request(method, f"http://openrouter{self.path}", content=body))

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        self._send(self._route("GET", b""))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self._send(self._route("POST", self.rfile.read(length)))

    def _send(self, response: httpx.Response) -> None:
        raw = response.content
        self.send_response(response.status_code)
        self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write(f"openrouter {self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104 — compose network only
