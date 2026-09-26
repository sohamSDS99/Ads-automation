"""Recorded model outputs for a creative run (PRD §17 CC9, CC16).

A cassette is every provider response one golden run consumed — chat
completions, image generations, video submits, polls and downloads — keyed by
WHAT WAS ASKED, not by when. A wave of independent nodes runs its calls
concurrently (`asyncio.Semaphore(4)`), so an order-keyed tape would make the
test depend on the scheduler; a request-keyed one does not.

The key is the SHA-256 of the request's canonical JSON with the values a
re-run legitimately changes normalised away: UUIDs (every row id is fresh),
timestamps and dates (offers are observed "an hour ago"), and 64-hex digests
(a brief hash covers ids). A request whose normalised form was never recorded
is a **miss** and raises — the node fails and the run with it, naming the
channel and the schema. A recorded response that the run never asked for is
**unplayed**, and the golden test asserts there are none: replay must be the
same conversation, not a subset of it.

A response may echo ids from its request (a pair label keyed by candidate id).
Recording replaces each UUID the response shares with its request by its
ordinal among the request's distinct UUIDs; replay substitutes the live
request's UUID at that ordinal.

Binary bodies are not inlined. A generated photograph is stored as its recipe
(`golden_creative.photo(size, seed)`) plus the SHA-256 of the exact bytes the
recording served — replay regenerates it and refuses a mismatch. A video
download is stored as the path of the recorded S4-P1 file plus its SHA-256.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

TESTS = Path(__file__).resolve().parents[1]
VERSION = 1
#: Values a re-run changes without the question changing.
_NORMALISERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
     "<uuid>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"),
     "<ts>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<date>"),
    (re.compile(r"\b[0-9a-f]{64}\b"), "<sha256>"),
)  # fmt: skip
_UUID = _NORMALISERS[0][0]
#: The response header a scripted source uses to say "these images are
#: `photo(size, seed)`" — recorded as the recipe, never sent on.
GENERATOR_HEADER = "x-cassette-photos"
#: GET channels are reads: once their recording is exhausted, the last
#: response is the answer (a finished job stays finished).
READS = frozenset({"video_poll", "video_content"})


class CassetteMiss(AssertionError):
    """The run asked something the cassette never recorded."""


def normalise(text: str) -> str:
    for pattern, placeholder in _NORMALISERS:
        text = pattern.sub(placeholder, text)
    return text


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _distinct_uuids(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in _UUID.findall(text):
        seen.setdefault(match.lower(), None)
    return list(seen)


def request_text(channel: str, request: httpx.Request) -> str:
    """The request as the key sees it: body JSON for a POST, the path for a GET."""
    if request.method == "GET":
        return _canonical({"channel": channel, "path": request.url.path})
    body = json.loads(request.content or b"{}")
    return _canonical({"channel": channel, "body": body})


def schema_name(request: httpx.Request) -> str | None:
    if request.method != "POST" or not request.content:
        return None
    body = json.loads(request.content)
    spec = (body.get("response_format") or {}).get("json_schema") or {}
    name = spec.get("name")
    return str(name) if name else None


class Cassette:
    def __init__(self, path: Path, entries: list[dict[str, Any]], *, recording: bool) -> None:
        self.path = path
        self.recording = recording
        self.entries = entries
        self._queues: dict[str, deque[int]] = defaultdict(deque)
        self._played: set[int] = set()
        self._last: dict[str, int] = {}
        for index, entry in enumerate(entries):
            self._queues[entry["key"]].append(index)

    # -- construction --------------------------------------------------------

    @classmethod
    def recording(cls, path: Path) -> Cassette:
        return cls(path, [], recording=True)

    @classmethod
    def load(cls, path: Path) -> Cassette:
        if not path.exists():
            raise CassetteMiss(
                f"no cassette at {path}; record it with GOLDEN_RECORD=1 (tests/cassettes/README.md)"
            )
        data = json.loads(path.read_text())
        assert data["version"] == VERSION, f"{path}: cassette version {data['version']}"
        return cls(path, list(data["interactions"]), recording=False)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": VERSION, "interactions": self.entries}
        self.path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")

    # -- the route -----------------------------------------------------------

    def route(
        self, channel: str, source: Callable[..., httpx.Response]
    ) -> Callable[..., httpx.Response]:
        """A respx side effect: record `source`'s answer, or replay the recorded one."""

        def handle(request: httpx.Request, **params: Any) -> httpx.Response:
            if self.recording:
                return self._record(channel, request, source(request, **params))
            return self._replay(channel, request)

        return handle

    def unplayed(self) -> list[dict[str, Any]]:
        """Recorded interactions the run never asked for (a replay is the same
        conversation, not a subset of it)."""
        return [
            {"channel": e["channel"], "schema": e.get("schema"), "key": e["key"]}
            for i, e in enumerate(self.entries)
            if i not in self._played and e["channel"] not in READS
        ]

    # -- recording -----------------------------------------------------------

    def _record(
        self, channel: str, request: httpx.Request, response: httpx.Response
    ) -> httpx.Response:
        text = request_text(channel, request)
        key = _sha(normalise(text))
        stored = self._store_body(response, _distinct_uuids(text))
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() in ("content-type", "retry-after")
        }
        self.entries.append(
            {
                "channel": channel,
                "schema": schema_name(request),
                "key": key,
                "request": _without_schema(normalise(text)),
                "status": response.status_code,
                "headers": headers,
                **stored,
            }
        )
        index = len(self.entries) - 1
        self._played.add(index)
        # What the run receives is exactly what replay will rebuild.
        return self._build(self.entries[index], _distinct_uuids(text))

    def _store_body(self, response: httpx.Response, uuids: list[str]) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            return {"file": _known_file(response.content)}
        body = response.json()
        photos = response.headers.get(GENERATOR_HEADER)
        if photos:
            recipes = json.loads(photos)
            for item, recipe in zip(body["data"], recipes, strict=True):
                data = base64.b64decode(item.pop("b64_json"))
                item["$photo"] = {
                    "size": recipe["size"],
                    "seed": recipe["seed"],
                    "sha256": _sha(data),
                }
        text = _canonical(body)
        for ordinal, value in enumerate(uuids):
            text = re.sub(re.escape(value), f"{{{{uuid:{ordinal}}}}}", text, flags=re.IGNORECASE)
        return {"json": text}

    # -- replay --------------------------------------------------------------

    def _replay(self, channel: str, request: httpx.Request) -> httpx.Response:
        text = request_text(channel, request)
        key = _sha(normalise(text))
        queue = self._queues.get(key)
        if queue:
            index = queue.popleft()
        elif channel in READS and key in self._last:
            index = self._last[key]
        else:
            name = schema_name(request)
            live = _without_schema(normalise(text))
            nearest = [
                _first_difference(e["request"], live)
                for e in self.entries
                if e["channel"] == channel and e.get("schema") == name
            ]
            raise CassetteMiss(
                f"cassette {self.path.name}: no recorded {channel} response for "
                f"{name or request.url.path} (key {key[:12]}); recorded requests of that "
                f"schema differ at: {nearest or 'none recorded'}. Re-record with "
                "GOLDEN_RECORD=1 if the request changed on purpose."
            )
        self._played.add(index)
        self._last[key] = index
        return self._build(self.entries[index], _distinct_uuids(text))

    def _build(self, entry: dict[str, Any], uuids: list[str]) -> httpx.Response:
        headers = dict(entry["headers"])
        if "file" in entry:
            content = _load_file(entry["file"])
            return httpx.Response(entry["status"], content=content, headers=headers)
        text = entry["json"]

        def restore(match: re.Match[str]) -> str:
            ordinal = int(match.group(1))
            if ordinal >= len(uuids):
                raise CassetteMiss(
                    f"cassette {self.path.name}: a {entry['channel']} response echoes request "
                    f"UUID #{ordinal}, but this request carries only {len(uuids)}"
                )
            return uuids[ordinal]

        body = json.loads(re.sub(r"\{\{uuid:(\d+)\}\}", restore, text))
        for item in body.get("data", []) if isinstance(body, dict) else []:
            if isinstance(item, dict) and "$photo" in item:
                item["b64_json"] = base64.b64encode(_photo(item.pop("$photo"))).decode()
        return httpx.Response(entry["status"], json=body, headers=headers)


def _without_schema(text: str) -> str:
    """The normalised request minus its JSON schema (recorded once per entry
    would triple a cassette; the key still covers it)."""
    value = json.loads(text)
    spec = ((value.get("body") or {}).get("response_format") or {}).get("json_schema")
    if isinstance(spec, dict) and "schema" in spec:
        spec["schema"] = _sha(_canonical(spec["schema"]))
    return _canonical(value)


def _first_difference(recorded: str, live: str, context: int = 80) -> str:
    at = next(
        (i for i, (a, b) in enumerate(zip(recorded, live, strict=False)) if a != b),
        min(len(recorded), len(live)),
    )
    return f"char {at}: recorded …{recorded[at - 20 : at + context]!r} vs live …{live[at - 20 : at + context]!r}"


def _photo(recipe: dict[str, Any]) -> bytes:
    from tests.integration.golden_creative import photo

    data = photo(tuple(recipe["size"]), int(recipe["seed"]))  # type: ignore[arg-type]
    if _sha(data) != recipe["sha256"]:
        raise CassetteMiss(
            f"photo(size={recipe['size']}, seed={recipe['seed']}) no longer produces the "
            "recorded bytes; the generator or its encoder changed — re-record"
        )
    return data


#: Binary bodies a cassette may reference instead of inlining.
_FILES = (
    "fixtures/openrouter/video_content_0.mp4",
    "fixtures/video/landscape_6s_silent.mp4",
    "fixtures/video/landscape_4s_tone.mp4",
    "fixtures/video/portrait_6s_silent.mp4",
    "fixtures/video/portrait_4s_silent.mp4",
)


def _known_file(content: bytes) -> dict[str, str]:
    digest = _sha(content)
    for relative in _FILES:
        if _sha((TESTS / relative).read_bytes()) == digest:
            return {"path": relative, "sha256": digest}
    raise AssertionError(
        f"a binary response ({len(content)} bytes, sha256 {digest[:12]}) is not a known "
        f"fixture file; add it to creative_cassette._FILES"
    )


def _load_file(ref: dict[str, str]) -> bytes:
    content = (TESTS / ref["path"]).read_bytes()
    if _sha(content) != ref["sha256"]:
        raise CassetteMiss(f"{ref['path']} changed since the cassette recorded it")
    return content
