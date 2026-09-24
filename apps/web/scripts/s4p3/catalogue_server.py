"""The recorded OpenRouter catalogue over HTTP, for S4-P3's isolated stack.

The `api` container reads its catalogue from `OPENROUTER_BASE_URL`; pointed
here, it gets S4-P1's recordings — served by the same `CatalogueReplay` the
integration suite installs, so the stack cannot drift from what the tests
replay, and nothing reaches the live API.

One control on top, for the one scenario a recording cannot hold: the catalogue
changing under a person who already chose a model (Law 36). `POST /__drift`
with `{"model_id", "field", "values"}` rewrites that model's supported values
for `field` — `supported_parameters[field]` for an image model, in the listing
and every endpoint, and `supported_<field>s` for a video model — until
`POST /__reset`. The api caches the catalogue in Redis, so the caller expires
the fresh keys after drifting (see `browser-check.mjs`).

Runs inside the api image: `python /app/s4p3/catalogue_server.py`.
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

sys.path.insert(0, "/app")

from tests.media.replay import REPLAY  # noqa: E402

PORT = 8099
_ENDPOINTS = re.compile(r"/images/models/(?P<model>.+)/endpoints$")
#: {model_id: {field: values}}
DRIFT: dict[str, dict[str, list[Any]]] = {}


def _drift_image(entry: dict[str, Any], changes: dict[str, list[Any]]) -> None:
    params = entry.setdefault("supported_parameters", {}) or {}
    for field, values in changes.items():
        params[field] = {"type": "enum", "values": values}
    entry["supported_parameters"] = params


def _apply(path: str, body: Any) -> Any:
    if not DRIFT or not isinstance(body, dict):
        return body
    if path.endswith("/images/models"):
        for entry in body.get("data") or []:
            if entry.get("id") in DRIFT:
                _drift_image(entry, DRIFT[entry["id"]])
    elif path.endswith("/videos/models"):
        for entry in body.get("data") or []:
            for field, values in DRIFT.get(entry.get("id"), {}).items():
                entry[f"supported_{field}s"] = values
    else:
        match = _ENDPOINTS.search(path)
        if match and match["model"] in DRIFT:
            for endpoint in body.get("endpoints") or []:
                _drift_image(endpoint, DRIFT[match["model"]])
    return body


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        request = httpx.Request("GET", f"http://catalogue{self.path}")
        response = REPLAY.handle(request)
        body = _apply(self.path.split("?", 1)[0], response.json())
        self._send(response.status_code, body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/__drift":
            DRIFT.setdefault(payload["model_id"], {})[payload["field"]] = payload["values"]
            self._send(200, {"drift": DRIFT})
        elif self.path == "/__reset":
            DRIFT.clear()
            self._send(200, {"drift": DRIFT})
        else:
            self._send(404, {"error": "not found"})

    def _send(self, status: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write(
            f"catalogue {self.command} {self.path} {args[1] if len(args) > 1 else ''}\n"
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104 — compose network only
