"""`web_crawler`'s Stage 03 extension: claim sections and offer blocks (§10.1).

3.2.1 harvests "what we already say, everywhere, including copy nobody
remembers writing", and 3.2.4 validates offers "against live offer data". Both
arrive in S3-P3; what this phase owes them is the evidence, and evidence they
cannot address is evidence they cannot cite.

So each section carries **where on the page it was** — the heading it sat under
and the element that held it. A claim quoted without that is a claim a legal
owner cannot go and check before signing for it.
"""

from __future__ import annotations

from agent.connectors.web_crawler import CLAIM_SECTION, OFFER_BLOCK, WebCrawlerConnector

PAGE_HTML = """
<html><body>
  <h1>Safety data sheets, sorted</h1>
  <section>
    <h2>Why teams choose us</h2>
    <p>We are the leading SDS platform, trusted by 4,000+ companies.</p>
  </section>
  <section>
    <h2>Pricing</h2>
    <p>Plans from £49 a month. Save 20% when you pay annually.</p>
  </section>
  <section>
    <h2>About</h2>
    <p>We were founded in Oslo and we like coffee.</p>
  </section>
</body></html>
"""


def sections(kind: str) -> list:
    connector = WebCrawlerConnector()
    return [
        draft
        for draft in connector.stage_three_drafts("https://sdsmanager.com/", PAGE_HTML)
        if draft.kind == kind
    ]


class TestClaimSections:
    def test_a_claim_bearing_section_is_extracted(self) -> None:
        texts = [draft.content_text or "" for draft in sections(CLAIM_SECTION)]

        assert any("leading SDS platform" in text for text in texts)

    def test_one_block_can_be_both_a_claim_and_an_offer(self) -> None:
        """ "Save 20%" is a quantified claim *and* a percent-off construction.

        3.2.1 has to sign for the number and 3.2.4 has to check it against live
        offer data, so both need the row. Emitting it once under one kind would
        mean one of them never sees it.
        """
        claims = {d.content_text for d in sections(CLAIM_SECTION)}
        offers = {d.content_text for d in sections(OFFER_BLOCK)}

        assert claims & offers

    def test_prose_with_no_claim_in_it_is_not(self) -> None:
        """Harvesting everything would bury 3.2.1 in copy nobody has to sign for."""
        texts = " ".join(draft.content_text or "" for draft in sections(CLAIM_SECTION))

        assert "we like coffee" not in texts.lower()

    def test_a_section_says_which_detectors_fired(self) -> None:
        payload = sections(CLAIM_SECTION)[0].payload

        families = set(payload["families"])
        assert "superlative" in families
        assert "quantified" in families

    def test_a_section_carries_where_it_was(self) -> None:
        """A claim a legal owner cannot find on the page is one they cannot check."""
        payload = sections(CLAIM_SECTION)[0].payload

        assert payload["url"] == "https://sdsmanager.com/"
        assert payload["heading"] == "Why teams choose us"
        assert payload["selector"]


class TestOfferBlocks:
    def test_a_price_block_is_extracted(self) -> None:
        found = sections(OFFER_BLOCK)

        assert len(found) == 1
        assert "£49" in (found[0].content_text or "")

    def test_the_constructions_it_found_are_named(self) -> None:
        """3.2.4's rules are keyed by construction, so the evidence names them."""
        payload = sections(OFFER_BLOCK)[0].payload

        assert set(payload["constructions"]) == {"from_price", "percent_off"}

    def test_a_section_with_no_offer_is_not_one(self) -> None:
        texts = " ".join(draft.content_text or "" for draft in sections(OFFER_BLOCK))

        assert "founded in Oslo" not in texts


class TestOptIn:
    def test_the_stage_three_kinds_are_not_emitted_unless_asked_for(self) -> None:
        """Stage 01 crawls every project. It must not start paying for this."""
        connector = WebCrawlerConnector()
        kinds = {"page"}

        assert CLAIM_SECTION not in kinds
        assert connector.stage_three_wanted(kinds) is False
        assert connector.stage_three_wanted({"page", CLAIM_SECTION}) is True


class TestWiring:
    """A method nothing calls is the written-never-read trap in miniature."""

    async def test_a_crawl_asked_for_claim_sections_emits_them(self) -> None:
        import httpx

        from agent.connectors.base import ConnectorContext

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=PAGE_HTML, headers={"content-type": "text/html; charset=utf-8"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        connector = WebCrawlerConnector(ConnectorContext(client=client))
        try:
            drafts = await connector.fetch(
                {
                    "domain": "sdsmanager.com",
                    "urls": ["https://sdsmanager.com/"],
                    "kinds": ["page", CLAIM_SECTION, OFFER_BLOCK],
                    "max_urls": 1,
                    "max_depth": 0,
                }
            )
        finally:
            await client.aclose()

        kinds = {draft.kind for draft in drafts}
        assert "page" in kinds
        assert CLAIM_SECTION in kinds
        assert OFFER_BLOCK in kinds

    async def test_an_ordinary_stage_one_crawl_emits_neither(self) -> None:
        """Stage 01 crawls every project and must not start paying for this."""
        import httpx

        from agent.connectors.base import ConnectorContext

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=PAGE_HTML, headers={"content-type": "text/html; charset=utf-8"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        connector = WebCrawlerConnector(ConnectorContext(client=client))
        try:
            drafts = await connector.fetch(
                {
                    "domain": "sdsmanager.com",
                    "urls": ["https://sdsmanager.com/"],
                    "max_urls": 1,
                    "max_depth": 0,
                }
            )
        finally:
            await client.aclose()

        kinds = {draft.kind for draft in drafts}
        assert kinds == {"page"}
