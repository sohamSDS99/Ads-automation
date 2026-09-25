"""A local web server for the three landing fixture pages (Stage 04 S4-P7).

It serves `tests/fixtures/landing/` on 127.0.0.1 and records every request that
reaches it — method, path, and when it arrived and finished — so a test can
assert from the *server's* side that the renderer issued nothing but GETs. The
renderer's own route interception is the guard; this log is the witness.

Any method is answered (an empty 200), so a write that slipped past the guard
would be recorded here rather than bounced by the server and missed.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "landing"

MISMATCHED_H1 = "mismatched_h1.html"
OFFER_BELOW_FOLD = "offer_below_fold.html"
NINE_FIELD_FORM = "nine_field_form.html"


@dataclass(frozen=True, slots=True)
class Hit:
    method: str
    path: str
    started: float
    finished: float


@dataclass
class FixtureServer:
    base_url: str
    hits: list[Hit] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def url(self, name: str) -> str:
        return f"{self.base_url}/{name}"

    def methods(self) -> set[str]:
        with self.lock:
            return {hit.method for hit in self.hits}

    def pages(self) -> list[Hit]:
        """Document requests only, in arrival order."""
        with self.lock:
            return [hit for hit in self.hits if hit.path.endswith(".html")]


def _handler(server: FixtureServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            started = time.monotonic()
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            target = (FIXTURES / path.lstrip("/")).resolve()
            if self.command == "GET" and path.startswith("/redirect/"):
                # `/redirect/<page>` → 302 to `/<page>`: the final URL after
                # redirects is one of the facts the renderer records.
                body = b""
                self.send_response(302)
                self.send_header("Location", "/" + path.removeprefix("/redirect/"))
            elif self.command == "GET" and target.is_file() and target.parent == FIXTURES.resolve():
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif self.command == "GET":
                body = b"not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
            else:
                body = b""
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            with server.lock:
                server.hits.append(Hit(self.command, path, started, time.monotonic()))

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _answer

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return Handler


@contextmanager
def fixture_server() -> Iterator[FixtureServer]:
    state = FixtureServer(base_url="")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
