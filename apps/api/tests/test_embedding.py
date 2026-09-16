"""The embedder contract: width, determinism, and the schema it has to match."""

from __future__ import annotations

import math

import pytest

from agent.config import Settings, get_settings
from agent.db.models import EMBEDDING_DIM
from agent.evidence.embedding import (
    QUERY_PREFIX,
    FastEmbedEmbedder,
    HashingEmbedder,
    build_embedder,
    get_embedder,
)

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="


def norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def test_the_hashing_embedder_matches_the_column_width() -> None:
    """A provider narrower or wider than the column fails at INSERT, far from the cause."""
    assert HashingEmbedder().dim == EMBEDDING_DIM


def test_vectors_are_unit_length() -> None:
    """Cosine distance and inner product only agree on normalised vectors."""
    vectors = HashingEmbedder().embed(["safety data sheet software", "ghs labelling"])
    for vector in vectors:
        assert norm(vector) == pytest.approx(1.0)


def test_embedding_is_deterministic() -> None:
    first = HashingEmbedder().embed(["chemical inventory"])
    second = HashingEmbedder().embed(["chemical inventory"])
    assert first == second


def test_empty_input_returns_no_vectors() -> None:
    assert HashingEmbedder().embed([]) == []


def test_an_empty_string_does_not_divide_by_zero() -> None:
    vector = HashingEmbedder().embed([""])[0]
    assert len(vector) == EMBEDDING_DIM
    assert norm(vector) == 0.0


def test_shared_vocabulary_ranks_above_unrelated_text() -> None:
    """The floor this provider has to clear to be usable as a test double."""
    embedder = HashingEmbedder()
    query = embedder.embed_query("safety data sheet software")
    near, far = embedder.embed(
        ["safety data sheet software for manufacturing", "tuesday afternoon pastry recipes"]
    )
    assert sum(a * b for a, b in zip(query, near, strict=True)) > sum(
        a * b for a, b in zip(query, far, strict=True)
    )


def test_order_is_preserved() -> None:
    """`store.write` zips vectors against drafts positionally; a reorder mislabels every row."""
    embedder = HashingEmbedder()
    texts = ["alpha alpha", "beta beta", "gamma gamma"]
    batch = embedder.embed(texts)
    assert batch == [embedder.embed([text])[0] for text in texts]


def test_the_query_prefix_is_the_bge_one() -> None:
    """`bge-*` was trained with an instruction on the query side only."""
    assert QUERY_PREFIX.startswith("Represent this sentence for searching")


def test_provider_choice_follows_configuration() -> None:
    hashed = build_embedder(Settings(app_encryption_key=TEST_KEY, embedding_provider="hash"))
    assert isinstance(hashed, HashingEmbedder)
    local = build_embedder(Settings(app_encryption_key=TEST_KEY, embedding_provider="fastembed"))
    assert isinstance(local, FastEmbedEmbedder)


def test_the_unit_suite_never_configures_the_onnx_model() -> None:
    """Guards the conftest setting. A model download in CI is a 400MB surprise."""
    assert get_settings().embedding_provider == "hash"
    assert isinstance(get_embedder(), HashingEmbedder)


def test_fastembed_is_lazy() -> None:
    """Constructing the production embedder must not load the model.

    The API process builds one on import of the search module and may never
    embed anything; paying a few hundred megabytes for that would be absurd.
    """
    import sys

    sys.modules.pop("fastembed", None)
    embedder = FastEmbedEmbedder("BAAI/bge-small-en-v1.5")
    assert embedder.name == "BAAI/bge-small-en-v1.5"
    assert "fastembed" not in sys.modules


@pytest.mark.skipif(
    not __import__("importlib.util", fromlist=["util"]).find_spec("fastembed"),
    reason="fastembed is not installed",
)
def test_fastembed_width_matches_the_column() -> None:
    """The real model, when the machine has it cached. Downloads on a cold run."""
    pytest.importorskip("fastembed")
    import os

    if os.environ.get("ARA_SKIP_MODEL_DOWNLOAD", "1") == "1":
        pytest.skip("set ARA_SKIP_MODEL_DOWNLOAD=0 to exercise the real model")
    vectors = FastEmbedEmbedder("BAAI/bge-small-en-v1.5").embed(["safety data sheets"])
    assert len(vectors[0]) == EMBEDDING_DIM
