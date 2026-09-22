"""The `brand_book` connector: spans with provenance, assets, colours, degradation.

PRD §10.2. The connector's whole job is to turn a binary nobody may put in a
prompt into text spans somebody can check — so almost every test here is about
provenance rather than about text. A span whose page and box are wrong is worse
than a missing span: it sends a brand owner to the wrong part of their own
document to confirm a rule.
"""

from __future__ import annotations

import pytest

from agent.connectors.base import ConnectorDegraded
from agent.connectors.brand_book import BrandBookConnector
from tests.brand_book_support import docx_bytes, image_only_pdf, pdf_bytes, png_bytes, pptx_bytes


def extract(filename: str, content: bytes) -> list:
    return BrandBookConnector().extract(filename, content, asset_path="vol/brand/1")


class TestPdfSpans:
    def test_a_pdf_yields_one_span_per_text_run_with_its_page(self) -> None:
        content = pdf_bytes(
            [
                [("Our voice is plain and exact.", 72, 700)],
                [("Never say cheap. Say good value.", 72, 640)],
            ]
        )

        spans = extract("brand.pdf", content)

        assert [span.content_text for span in spans] == [
            "Our voice is plain and exact.",
            "Never say cheap. Say good value.",
        ]
        assert [span.payload["page"] for span in spans] == [1, 2]

    def test_every_span_carries_a_bounding_box(self) -> None:
        """G5 asks a brand owner to confirm a rule. They need to find it in the book."""
        spans = extract("brand.pdf", pdf_bytes([[("Clear space is 1x the logo height.", 90, 612)]]))

        box = spans[0].payload["bbox"]

        assert len(box) == 4
        assert box[0] == 90
        assert box[1] == 612
        assert box[2] > box[0]
        assert box[3] > box[1]

    def test_the_draft_is_stamped_brand_book_span(self) -> None:
        spans = extract("brand.pdf", pdf_bytes([[("Anything.", 10, 10)]]))

        assert spans[0].kind == "brand_book_span"


class TestDocx:
    def test_a_docx_yields_a_span_per_paragraph_indexed_not_boxed(self) -> None:
        """Word has no page box until it is laid out. Inventing one would be a lie."""
        spans = extract("brand.docx", docx_bytes(["Tone: direct.", "Never shout."]))

        assert [span.content_text for span in spans] == ["Tone: direct.", "Never shout."]
        assert [span.payload["paragraph"] for span in spans] == [1, 2]
        assert spans[0].payload["bbox"] is None

    def test_blank_paragraphs_do_not_become_spans(self) -> None:
        spans = extract("brand.docx", docx_bytes(["Tone: direct.", "   ", ""]))

        assert len(spans) == 1


class TestPptx:
    def test_a_pptx_span_carries_a_true_box_from_the_shape(self) -> None:
        """The one format whose box is measured rather than estimated."""
        spans = extract(
            "brand.pptx",
            pptx_bytes([[("Logo clear space: 1x cap height.", 914400, 457200, 3657600, 685800)]]),
        )

        assert spans[0].content_text == "Logo clear space: 1x cap height."
        assert spans[0].payload["page"] == 1
        assert spans[0].payload["bbox"] == [914400, 457200, 4572000, 1143000]
        assert spans[0].payload["bbox_is_estimated"] is False


class TestDegradation:
    def test_an_unreadable_format_degrades_rather_than_raising_connector_error(self) -> None:
        """§10.2: the run continues and the rulebook says the book was not read."""
        with pytest.raises(ConnectorDegraded) as caught:
            extract("brand.sketch", b"\x00\x01binary")

        assert "brand.sketch" in str(caught.value)

    def test_a_pdf_with_no_text_layer_degrades_and_says_why(self) -> None:
        """A scan of a brochure. pypdf reports success and zero characters."""
        with pytest.raises(ConnectorDegraded) as caught:
            extract("scan.pdf", image_only_pdf())

        assert "no text" in str(caught.value).lower()

    def test_a_degraded_file_still_hands_back_what_it_did_read(self) -> None:
        with pytest.raises(ConnectorDegraded) as caught:
            extract("scan.pdf", image_only_pdf())

        assert caught.value.drafts == []


class TestImages:
    def test_a_loose_image_becomes_a_logo_asset_and_a_colour_token(self) -> None:
        drafts = extract("logo.png", png_bytes((194, 65, 12)))

        kinds = {draft.kind for draft in drafts}
        assert "brand_book_asset" in kinds
        assert "brand_book_colour" in kinds

    def test_the_colour_token_is_the_hex_of_the_dominant_pixel(self) -> None:
        drafts = extract("logo.png", png_bytes((194, 65, 12)))
        colours = [d for d in drafts if d.kind == "brand_book_colour"]

        assert colours[0].payload["hex"] == "#c2410c"

    def test_an_image_asset_names_its_path_and_never_carries_its_bytes(self) -> None:
        """Law 30. The bytes stay on the Volume; evidence points at them."""
        drafts = extract("logo.png", png_bytes((0, 0, 0)))
        asset = next(d for d in drafts if d.kind == "brand_book_asset")

        assert asset.payload["asset_path"] == "vol/brand/1"
        assert not any(isinstance(value, bytes) for value in asset.payload.values())


class TestEmbeddedAssets:
    def test_an_image_embedded_in_a_pdf_becomes_a_candidate_logo(self) -> None:
        """A logo lives inside the brand book far more often than beside it."""
        drafts = extract("brand.pdf", _pdf_with_text_and_image())
        assets = [d for d in drafts if d.kind == "brand_book_asset"]

        assert len(assets) == 1
        assert assets[0].payload["page"] == 1


class TestFetch:
    async def test_fetch_reads_every_file_it_is_given(self) -> None:
        drafts = await BrandBookConnector().fetch(
            {
                "files": [
                    {
                        "filename": "a.pdf",
                        "content": pdf_bytes([[("A.", 10, 10)]]),
                        "asset_path": "p/a",
                    },
                    {
                        "filename": "b.pdf",
                        "content": pdf_bytes([[("B.", 10, 10)]]),
                        "asset_path": "p/b",
                    },
                ]
            }
        )

        assert [d.content_text for d in drafts] == ["A.", "B."]

    async def test_one_unreadable_file_degrades_but_keeps_the_others(self) -> None:
        """§10.2: `degraded_sources += brand_book`, and the run continues."""
        with pytest.raises(ConnectorDegraded) as caught:
            await BrandBookConnector().fetch(
                {
                    "files": [
                        {
                            "filename": "good.pdf",
                            "content": pdf_bytes([[("Kept.", 10, 10)]]),
                            "asset_path": "p/a",
                        },
                        {"filename": "scan.pdf", "content": image_only_pdf(), "asset_path": "p/b"},
                    ]
                }
            )

        assert [d.content_text for d in caught.value.drafts] == ["Kept."]
        assert "scan.pdf" in caught.value.reason


class TestRegistry:
    def test_brand_book_is_in_the_connector_registry(self) -> None:
        from agent.connectors import CONNECTOR_NAMES, connector_class, credential_kind_for

        assert "brand_book" in CONNECTOR_NAMES
        assert connector_class("brand_book") is BrandBookConnector
        assert credential_kind_for("brand_book") is None

    def test_brand_book_is_read_only(self) -> None:
        """A guideline node names it, and the executor refuses an unmarked connector."""
        from agent.connectors import is_read_only

        assert is_read_only("brand_book") is True


def _pdf_with_text_and_image() -> bytes:
    import zlib

    from tests.brand_book_support import _assemble

    pixels = zlib.compress(b"\xc2\x41\x0c" * 16)
    stream = b"BT /F1 12 Tf 72 700 Td (Our logo.) Tj ET"
    return _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 6 0 R >> /XObject << /Im0 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /XObject /Subtype /Image /Width 4 /Height 4 /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /FlateDecode /Length "
            + str(len(pixels)).encode()
            + b" >>\nstream\n"
            + pixels
            + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
    )
