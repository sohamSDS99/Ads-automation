"""OpenRouter for S4-P18's isolated stack. Nothing here reaches the live API.

- `GET /api/v1/images/models`, `/videos/models`, `/images/models/{id}/endpoints`
  — S4-P1's recorded catalogue, through the suite's own `CatalogueReplay`
  (as S4-P3's stack), so the start route prices against real recordings.
- `GET /api/v1/models` — the suite's fake text catalogue (`FakeOpenRouter`).
- `POST /api/v1/chat/completions` — `writer`, by the schema a node sends:
  4.1.1's brief from `brief_writer` (every ad-group slot exactly once, in
  fixed words, so the brief page is the same on every run), and 4.2.1's
  headline pool and pair labels from S4-P5's own scripted answers. Any other
  schema is refused by name, so a node that starts calling a model shows up
  as a failure naming it rather than as a plausible guess.
- `GET /api/v1/videos/{id}` and `/content` — S4-P1's recorded completed poll
  and mp4, so a timed-out video that "Check again" re-polls completes for real
  in the worker.

`seed.py` imports `FAKE` to take a run to G7 in-process. Runs inside
the api image: `python /app/s4p18/openrouter_server.py`.
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

sys.path.insert(0, "/app")

from tests.integration.test_s4p5_headlines_combinations import POOL, _Script  # noqa: E402
from tests.media.openrouter_mock import recorded, video_bytes  # noqa: E402
from tests.media.replay import REPLAY  # noqa: E402
from tests.openrouter_fake import FakeOpenRouter, completion  # noqa: E402

PORT = 8099
_VIDEO = re.compile(r"/api/v1/videos/(?P<job>[^/]+)(?P<content>/content)?$")

#: The words of the brief, by the property that holds them. Fixed, so a visual
#: baseline taken on one run holds on the next.
WORDS: dict[str, list[str]] = {
    "objective": [
        "Win qualified leads from EHS managers who have to keep every safety data sheet "
        "current across several sites."
    ],
    "audience": [
        "EHS managers at chemical distributors who answer for every sheet at audit time.",
        "Operations leads replacing binders and shared drives with one searchable library.",
    ],
    "exclusions": ["Students and researchers looking for a single sheet to download."],
    "angle": ["Audit-ready safety data sheets, without chasing suppliers for updates."],
    "theme": [
        "SDS management software",
        "SDS app for the plant floor",
        "Chemical inventory with SDS attached",
    ],
    "primary_message": [
        "Keep every safety data sheet current, automatically.",
        "Every sheet on every phone, even offline in the plant.",
        "Know what you store and have its sheet ready.",
    ],
    "angle_b": [
        "Pass the next audit without a binder.",
        "Scan a label, get the sheet.",
        "One inventory, every sheet attached.",
    ],
}

#: Which stage a line of each kind prefers to stand on, so the chips vary the
#: way a real brief's do.
PREFER: dict[str, str] = {
    "objective": "S2",
    "audience": "S1",
    "exclusions": "S1",
    "angle": "S3",
    "primary_message": "S2",
    "angle_b": "S1",
}


def _resolve(root: dict[str, Any], ref: str) -> dict[str, Any]:
    node: Any = root
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return dict(node)


def _deref(node: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = _resolve(root, node["$ref"])
    if "anyOf" in node:
        options = [item for item in node["anyOf"] if item.get("type") != "null"]
        return _deref(options[0], root)
    return node


def _answer(node: dict[str, Any], root: dict[str, Any], name: str, index: int) -> Any:
    node = _deref(node, root)
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][index % len(node["enum"])]
    kind = node.get("type")
    if kind == "object":
        return _object(node, root, name, index)
    if kind == "array":
        return _array(node, root, name)
    if kind == "string":
        words = WORDS.get(name)
        return words[index % len(words)] if words else "Keep every safety data sheet current"
    if kind == "integer":
        return 1
    if kind == "boolean":
        return False
    raise ValueError(f"no answer for {node}")


def _object(node: dict[str, Any], root: dict[str, Any], name: str, index: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in node["properties"].items():
        if key == "text":
            words = WORDS.get(name) or ["Keep every safety data sheet current"]
            out[key] = words[index % len(words)]
        elif key == "source_keys":
            out[key] = [_source_key(value, root, name)]
        else:
            out[key] = _answer(value, root, key, index)
    return out


def _array(node: dict[str, Any], root: dict[str, Any], name: str) -> list[Any]:
    items = _deref(node["items"], root)
    if name == "ad_groups":
        # 4.1.1 must brief every slot exactly once: one item per slot key.
        slots = _deref(items["properties"]["slot"], root)["enum"]
        return [_answer(items, root, name, i) | {"slot": slot} for i, slot in enumerate(slots)]
    if name == "proof_point_claim_ids":
        return list(_deref(items, root).get("enum") or [])
    count = len(WORDS.get(name) or [None])
    return [_answer(items, root, name, i) for i in range(count)]


def _source_key(node: dict[str, Any], root: dict[str, Any], name: str) -> str:
    keys: list[str] = list(_deref(node["items"], root)["enum"])
    stage = PREFER.get(name)
    preferred = [key for key in keys if stage and key.startswith(stage)]
    return (preferred or keys)[0]


def brief_writer(schema: dict[str, Any]) -> dict[str, Any]:
    return _object(_deref(schema, schema), schema, "brief", 0)


def writer(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    spec = body["response_format"]["json_schema"]
    name, schema = spec["name"], spec["schema"]
    if name == "CreativeBriefDraft":
        return completion(brief_writer(schema), model=body["model"])
    if name == "HeadlinePoolDraft":
        return completion({"candidates": POOL}, model=body["model"])
    if name == "PairLabelsDraft":
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        pairs = json.loads(user.split("PAIRS:\n", 1)[1])
        return completion({key: _Script._label(pair) for key, pair in pairs.items()})
    return httpx.Response(500, json={"error": {"message": f"no scripted answer for {name}"}})


FAKE = FakeOpenRouter()
FAKE.dispatch(writer)


def _video(job: str, content: bool) -> httpx.Response:
    if content:
        return httpx.Response(200, content=video_bytes(), headers={"content-type": "video/mp4"})
    status, poll = recorded("video_poll_completed.json")
    return httpx.Response(status, json={**poll, "id": job})


class Handler(BaseHTTPRequestHandler):
    def _route(self, method: str, body: bytes) -> httpx.Response:
        url = f"http://openrouter{self.path}"
        path = self.path.split("?", 1)[0]
        request = httpx.Request(method, url, content=body)
        video = _VIDEO.search(path)
        if video and video["job"] != "models":
            return _video(video["job"], bool(video["content"]))
        if path.endswith("/api/v1/models") or path.endswith("/chat/completions"):
            return FAKE.handler(request)
        return REPLAY.handle(request)

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
