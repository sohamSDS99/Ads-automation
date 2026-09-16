"""Text → vector. One provider interface, two implementations.

OpenRouter is this application's only LLM surface (PRD assumption A2), and it
serves no embedding model — its catalogue is chat-only. PRD §20 Q6 anticipated
that and named the fallback: a local `bge-small` through `fastembed`. That is
what production runs, so no second vendor and no second API key enters the
system for the sake of 384 floats.

`HashingEmbedder` exists for tests and for an offline CI. It is deterministic
and needs no model download, but it encodes token overlap rather than meaning —
never configure it in production.
"""

from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from typing import Protocol, runtime_checkable

import structlog

from agent.config import Settings, get_settings
from agent.db.models import EMBEDDING_DIM

log = structlog.get_logger(__name__)

#: `bge-*` models were trained with an instruction prefix on the query side only.
#: Omitting it costs a few points of retrieval quality, so `search.py` embeds the
#: user's query through `embed_query` and the corpus through `embed`.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class EmbeddingError(RuntimeError):
    """The embedder could not produce vectors."""


@runtime_checkable
class Embedder(Protocol):
    """Produces unit-norm vectors of exactly `EMBEDDING_DIM` floats."""

    @property
    def dim(self) -> int: ...

    @property
    def name(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed documents. Order of the result matches the order of the input."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed one search query."""
        ...


def _unit(vector: list[float]) -> list[float]:
    """Scale to unit length so cosine distance and inner product agree."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


class HashingEmbedder:
    """A deterministic bag-of-tokens embedder. No model, no network, no meaning.

    Each token is hashed to a bucket and its count accumulated, then the vector
    is L2-normalised. Two texts sharing vocabulary land near each other, which
    is enough to prove that storage, indexing and ranking are wired correctly —
    and it will never be enough to rank real evidence well.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return "hash"

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for token in text.lower().split():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest, "big") % self._dim
            vector[bucket] += 1.0
        return _unit(vector)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)


class FastEmbedEmbedder:
    """`fastembed` over ONNX runtime. The production embedder.

    The model is loaded once per process and is a few hundred megabytes
    resident, so it is built lazily — importing this module must stay cheap for
    the API process, which only embeds on a search request.
    """

    def __init__(self, model_name: str, *, batch_size: int = 64) -> None:
        self._model_name = model_name
        self._batch_size = batch_size
        self._model: object | None = None

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    @property
    def name(self) -> str:
        return self._model_name

    def _loaded(self) -> object:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise EmbeddingError(
                    "fastembed is not installed; set EMBEDDING_PROVIDER=hash to run without it"
                ) from exc
            log.info("embedding.model_loading", model=self._model_name)
            self._model = TextEmbedding(model_name=self._model_name)
            log.info("embedding.model_loaded", model=self._model_name)
        return self._model

    def _run(self, texts: list[str]) -> list[list[float]]:
        model = self._loaded()
        raw = model.embed(texts, batch_size=self._batch_size)  # type: ignore[attr-defined]
        vectors = [list(map(float, vector)) for vector in raw]
        for vector in vectors:
            if len(vector) != EMBEDDING_DIM:
                raise EmbeddingError(
                    f"{self._model_name} produced {len(vector)} dimensions, but the "
                    f"`evidence.embedding` column is vector({EMBEDDING_DIM}). "
                    "Changing the model is a migration, not a setting."
                )
        # fastembed normalises already; doing it again is cheap and makes the
        # guarantee this module advertises true regardless of the model.
        return [_unit(vector) for vector in vectors]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._run(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._run([QUERY_PREFIX + text])[0]


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "hash":
        return HashingEmbedder()
    return FastEmbedEmbedder(settings.embedding_model, batch_size=settings.embedding_batch_size)


@lru_cache(maxsize=1)
def _cached(provider: str, model: str, batch_size: int) -> Embedder:
    if provider == "hash":
        return HashingEmbedder()
    return FastEmbedEmbedder(model, batch_size=batch_size)


def get_embedder(settings: Settings | None = None) -> Embedder:
    """The process-wide embedder. Cached on its configuration, not on identity."""
    settings = settings or get_settings()
    return _cached(
        settings.embedding_provider, settings.embedding_model, settings.embedding_batch_size
    )
