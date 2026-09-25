"""OpenRouter for S4-P19's isolated stack. Nothing here reaches the live API.

Every answer is S4-P6's scripted OpenRouter (`test_s4p6_descriptions_variant_b._Script`),
so the run the Ad Studio shows is the one S4-P6's suite proves: 4.1.1's brief
with every ad-group slot briefed once, 4.2.1's and 4.2.2's pools for A and
for B, and 4.2.3's pair labels — with one change, `ORDERED`: the first pair
of variant A's first batch is labelled `order_dependent`, so 4.2.3 pins it
(`combinatorics.pins_v1`) and the studio shows a real pin rather than none.

`seed.py` imports `SCRIPT` to execute a run in-process; the same script also
serves HTTP for anything in the api that asks. Runs inside the api image:
`python /app/s4p19/openrouter_server.py`.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

sys.path.insert(0, "/app")

from tests.integration.test_s4p6_descriptions_variant_b import _Script  # noqa: E402
from tests.integration.variant_b_support import TEXTS_B  # noqa: E402
from tests.openrouter_fake import completion  # noqa: E402

PORT = 8099


class _Ordered(_Script):
    """S4-P6's script, with one variant-A pair that reads in one order only."""

    def __init__(self) -> None:
        super().__init__()
        self.ordered = False

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["response_format"]["json_schema"]["name"] == "PairLabelsDraft":
            user = next(m["content"] for m in body["messages"] if m["role"] == "user")
            pairs: dict[str, dict[str, Any]] = json.loads(user.split("PAIRS:\n", 1)[1])
            labels = {key: "reads_well" for key in pairs}
            first = next(iter(pairs))
            is_a = not ({pairs[first]["a"], pairs[first]["b"]} & TEXTS_B)
            if not self.ordered and is_a and pairs[first]["kind"] == "HH":
                labels[first] = "order_dependent"
                self.ordered = True
            return completion(labels)
        return super()._respond(request)


#: `_Script.__init__` dispatches `self._respond`, which is the override here.
SCRIPT = _Ordered()


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
