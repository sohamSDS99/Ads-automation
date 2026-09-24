"""`media/references.py` — Law 44 and `product_depiction`, in code (PRD §10.3, §11 4.4.1, §13).

The six-case table is the phase's exit criterion: `product_depiction` is resolved
from capability × references × Law 44, never by a model. Every other test here
pins one clause of Law 44 on its own, so a regression names the clause it broke.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from PIL import Image

from agent.media.references import (
    ReferenceFacts,
    ReferenceRejected,
    accepts_image_input,
    inspect_upload,
    product_depiction,
    refusal,
    select_references,
    storage_key,
)
from agent.media.types import CapabilityRecord, Descriptor

MAX = 8 * 1024 * 1024


def _capability(*, image_input: bool = True, max_refs: int | None = 4) -> CapabilityRecord:
    params: dict[str, Descriptor] = {}
    if max_refs is not None:
        params["input_references"] = Descriptor(kind="range", min=0, max=max_refs)
    return CapabilityRecord(
        modality="image",
        model_id="vendor/image-model",
        params=params,
        input_modalities=["text", "image"] if image_input else ["text"],
    )


ACCEPTS = _capability()
TEXT_ONLY = _capability(image_input=False)


def _ref(
    origin: str = "own",
    *,
    kind: str = "product_reference",
    retired: bool = False,
    size: int | None = 1000,
    rights: str = "Shot by our studio in 2026; we hold all rights.",
) -> ReferenceFacts:
    return ReferenceFacts(
        reference_id=uuid.uuid4(),
        kind=kind,  # type: ignore[arg-type]
        origin=origin,  # type: ignore[arg-type]
        rights_statement=rights,
        bytes=size,
        retired=retired,
    )


OWN = _ref("own")
THIRD = _ref("third_party")


def _resolve(
    refs: list[ReferenceFacts],
    *,
    allowed: bool = True,
    capability: CapabilityRecord | None = ACCEPTS,
    cleared: frozenset[uuid.UUID] = frozenset(),
    images: bool = True,
) -> str:
    return product_depiction(
        refs,
        images_in_scope=images,
        allowed=allowed,
        capability=capability,
        cleared=cleared,
        max_bytes=MAX,
    )


# ---------------------------------------------------------------------------
# the six-case fixture table (S4-P9 exit criterion)
# ---------------------------------------------------------------------------

SIX_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("1 allowed, own reference, model takes images", {"refs": [OWN]}, "reference_guided"),
    (
        "2 allowed, own reference, model takes no image input",
        {"refs": [OWN], "capability": TEXT_ONLY},
        "composited_real",
    ),
    (
        "3 references not allowed (the default), own reference",
        {"refs": [OWN], "allowed": False},
        "composited_real",
    ),
    (
        "4 third-party reference, no cleared image_right",
        {"refs": [THIRD]},
        "composited_real",
    ),
    (
        "5 third-party reference, image_right cleared",
        {"refs": [THIRD], "cleared": frozenset({THIRD.reference_id})},
        "reference_guided",
    ),
    ("6 no product reference registered", {"refs": []}, "none"),
]


@pytest.mark.parametrize(("case", "given", "expected"), SIX_CASES, ids=[c[0] for c in SIX_CASES])
def test_product_depiction_six_case_table(case: str, given: dict[str, Any], expected: str) -> None:
    assert _resolve(**given) == expected


# ---------------------------------------------------------------------------
# the clauses the table is built from
# ---------------------------------------------------------------------------


def test_licensed_behaves_like_own() -> None:
    assert _resolve([_ref("licensed")]) == "reference_guided"


def test_a_style_reference_alone_puts_no_product_in_the_picture() -> None:
    assert _resolve([_ref(kind="style_reference")]) == "none"


def test_a_retired_product_reference_counts_as_none_registered() -> None:
    assert _resolve([_ref(retired=True)]) == "none"


def test_images_out_of_scope_resolve_to_none() -> None:
    assert _resolve([OWN], images=False) == "none"


def test_no_image_model_chosen_cannot_be_reference_guided() -> None:
    assert _resolve([OWN], capability=None) == "composited_real"


def test_a_clearance_for_another_reference_does_not_clear_this_one() -> None:
    assert _resolve([THIRD], cleared=frozenset({OWN.reference_id})) == "composited_real"


def test_one_sendable_reference_among_unsendable_ones_is_enough() -> None:
    assert _resolve([THIRD, OWN]) == "reference_guided"


def test_image_input_needs_the_modality_and_room_for_a_reference() -> None:
    # Exactly the rule `capability.validate()` applies to `input_references`: an
    # `image` input modality AND a range descriptor admitting at least one. Either
    # half alone would resolve `reference_guided` for a request the gateway then
    # refuses with a 422.
    assert accepts_image_input(ACCEPTS)
    assert not accepts_image_input(TEXT_ONLY)
    assert not accepts_image_input(_capability(max_refs=None))
    assert not accepts_image_input(_capability(max_refs=0))


# ---------------------------------------------------------------------------
# refusal — Law 44, one clause at a time
# ---------------------------------------------------------------------------


def _refusal(ref: ReferenceFacts, **overrides: Any) -> str | None:
    given: dict[str, Any] = {
        "allowed": True,
        "capability": ACCEPTS,
        "cleared": frozenset(),
        "max_bytes": MAX,
    }
    given.update(overrides)
    return refusal(ref, **given)


def test_an_own_attested_reference_may_go() -> None:
    assert _refusal(OWN) is None


def test_references_not_allowed_for_the_project() -> None:
    assert _refusal(OWN, allowed=False) == "references_not_allowed"


def test_a_model_without_image_input() -> None:
    assert _refusal(OWN, capability=TEXT_ONLY) == "model_takes_no_image_input"


def test_third_party_without_a_cleared_image_right() -> None:
    assert _refusal(THIRD) == "third_party_without_image_right"


def test_third_party_with_a_cleared_image_right() -> None:
    assert _refusal(THIRD, cleared=frozenset({THIRD.reference_id})) is None


def test_a_retired_reference_never_goes() -> None:
    assert _refusal(_ref(retired=True)) == "retired"


def test_a_blank_rights_statement_is_no_attestation() -> None:
    assert _refusal(_ref(rights="   ")) == "no_rights_attestation"


def test_over_the_size_cap() -> None:
    assert _refusal(_ref(size=MAX + 1)) == "over_size_cap"


def test_an_unknown_size_is_checked_where_the_bytes_are_read() -> None:
    # The CreativeInput snapshot carries no size; the loader measures the bytes.
    assert _refusal(_ref(size=None)) is None


# ---------------------------------------------------------------------------
# select_references — what one request may carry
# ---------------------------------------------------------------------------


def _select(refs: list[ReferenceFacts], depiction: str, **overrides: Any) -> list[uuid.UUID]:
    given: dict[str, Any] = {
        "allowed": True,
        "capability": ACCEPTS,
        "cleared": frozenset(),
        "max_bytes": MAX,
    }
    given.update(overrides)
    return [ref.reference_id for ref in select_references(refs, depiction, **given)]  # type: ignore[arg-type]


def test_reference_guided_sends_the_product_reference() -> None:
    assert _select([OWN], "reference_guided") == [OWN.reference_id]


def test_an_uncleared_third_party_reference_is_never_selected() -> None:
    # Resolved `reference_guided` because OWN may go — THIRD still may not.
    assert _select([THIRD, OWN], "reference_guided") == [OWN.reference_id]


def test_composited_real_and_none_send_no_product_reference() -> None:
    style = _ref(kind="style_reference")
    assert _select([OWN, style], "composited_real") == [style.reference_id]
    assert _select([OWN, style], "none") == [style.reference_id]


def test_products_first_then_style_capped_at_what_the_model_takes() -> None:
    style_a = _ref(kind="style_reference")
    style_b = _ref(kind="style_reference")
    chosen = _select(
        [style_a, OWN, style_b], "reference_guided", capability=_capability(max_refs=2)
    )
    assert chosen == [OWN.reference_id, style_a.reference_id]


def test_nothing_is_selected_when_references_may_not_leave() -> None:
    assert _select([OWN, _ref(kind="style_reference")], "reference_guided", allowed=False) == []


# ---------------------------------------------------------------------------
# uploads — what `POST /projects/{id}/media-references` accepts
# ---------------------------------------------------------------------------


def _image(fmt: str, size: tuple[int, int] = (12, 8)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("fmt", "media_type", "extension"),
    [("PNG", "image/png", "png"), ("JPEG", "image/jpeg", "jpg"), ("WEBP", "image/webp", "webp")],
)
def test_png_jpeg_and_webp_are_accepted(fmt: str, media_type: str, extension: str) -> None:
    content = _image(fmt)
    upload = inspect_upload(content, max_bytes=MAX)
    assert (upload.media_type, upload.extension) == (media_type, extension)
    assert (upload.width, upload.height) == (12, 8)
    assert upload.size == len(content)
    assert len(upload.sha256) == 64


def test_the_format_is_read_from_the_bytes_not_trusted_from_a_header() -> None:
    with pytest.raises(ReferenceRejected) as refused:
        inspect_upload(_image("GIF"), max_bytes=MAX)
    assert refused.value.code == "unsupported_format"


def test_an_empty_file() -> None:
    with pytest.raises(ReferenceRejected) as refused:
        inspect_upload(b"", max_bytes=MAX)
    assert refused.value.code == "empty"


def test_a_file_over_reference_max_bytes() -> None:
    content = _image("PNG")
    with pytest.raises(ReferenceRejected) as refused:
        inspect_upload(content, max_bytes=len(content) - 1)
    assert refused.value.code == "too_large"


@pytest.mark.parametrize("content", [b"not an image at all", _image("PNG")[:40]])
def test_bytes_that_do_not_decode(content: bytes) -> None:
    with pytest.raises(ReferenceRejected) as refused:
        inspect_upload(content, max_bytes=MAX)
    assert refused.value.code == "undecodable"


def test_storage_key_is_content_addressed_per_project() -> None:
    project = uuid.UUID("00000000-0000-4000-8000-000000000001")
    assert storage_key(project, "ab" * 32, "image/webp") == f"references/{project}/{'ab' * 32}.webp"
