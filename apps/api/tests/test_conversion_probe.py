"""What the synthetic conversion probe reads off a browser's network traffic.

The probe itself needs Chromium; the decision it makes does not. Everything
below is the classification of one request URL, which is the part that can be
wrong in a way nobody notices: a page that loads the Google tag but never fires
a conversion looks identical to a working one unless `beacon` distinguishes them.
"""

from __future__ import annotations

from agent.config import Settings
from agent.connectors.base import ConnectorContext
from agent.connectors.browser import classify_request
from agent.connectors.web_crawler import WebCrawlerConnector

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="

BEACON = (
    "https://www.googleadservices.com/pagead/conversion/987654321/"
    "?random=123&cv=11&label=AbC-D_efGhIjKlM&value=1"
)
FIRST_PARTY_BEACON = "https://www.google.com/pagead/1p-conversion/987654321/?label=AbC-D_efGhIjKlM"
LOADER = "https://www.googletagmanager.com/gtag/js?id=AW-987654321"
GTM = "https://www.googletagmanager.com/gtm.js?id=GTM-ABCDE12"


def test_a_conversion_beacon_is_recognised_with_its_send_to() -> None:
    seen = classify_request(BEACON)
    assert seen is not None
    assert seen["beacon"] is True
    assert seen["send_to"] == "AW-987654321/AbC-D_efGhIjKlM"


def test_the_first_party_spelling_counts_too() -> None:
    """Google moved the beacon to `1p-conversion` for cookie-restricted
    browsers. Matching only the old path reports a working tag as broken."""
    seen = classify_request(FIRST_PARTY_BEACON)
    assert seen is not None
    assert seen["beacon"] is True
    assert seen["send_to"] == "AW-987654321/AbC-D_efGhIjKlM"


def test_loading_the_tag_is_not_firing_a_conversion() -> None:
    """The distinction the whole probe exists for: the container loaded, the
    conversion did not. Counting the loader as a conversion would report every
    tagged page as converting."""
    seen = classify_request(LOADER)
    assert seen is not None
    assert seen["beacon"] is False
    assert seen["tag_id"] == "AW-987654321"
    assert seen["send_to"] is None


def test_a_gtm_container_is_recorded_as_a_container() -> None:
    seen = classify_request(GTM)
    assert seen is not None
    assert seen["tag_id"] == "GTM-ABCDE12"
    assert seen["beacon"] is False


def test_unrelated_traffic_is_not_recorded() -> None:
    assert classify_request("https://www.sdsmanager.com/us/pricing") is None
    assert classify_request("https://cdn.example.test/app.js") is None
    assert classify_request("") is None


def test_a_beacon_without_a_label_still_names_its_account() -> None:
    """A partial answer beats none: the conversion id alone tells you which
    account was billed even when the label is missing from the query string."""
    seen = classify_request("https://www.googleadservices.com/pagead/conversion/987654321/?cv=11")
    assert seen is not None
    assert seen["send_to"] == "AW-987654321"


async def test_the_probe_needs_a_url_and_says_so() -> None:
    """A probe with nowhere to fire is a degraded source, not a silent pass —
    node 1.5.2 turns this into `inconclusive` rather than `fail`."""
    crawler = WebCrawlerConnector(ConnectorContext(settings=Settings(app_encryption_key=TEST_KEY)))
    drafts, failures = await crawler._probe({})
    assert drafts == []
    assert failures == ["conversion_probe: no probe_url was supplied"]
