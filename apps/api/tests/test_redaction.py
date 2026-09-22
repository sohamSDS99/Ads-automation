"""Law 30: no PII in a prompt.

`redact_pii` is the deterministic pass that stands between creative history and
any model. These tests are the reason it can be trusted: each one names a shape
of personal data that really does appear in ad copy and site copy, and asserts
that the shape does not survive the pass.
"""

from __future__ import annotations

from agent.evidence.redact import redact_payload, redact_pii


class TestEmail:
    def test_removes_an_email_address_from_creative_copy(self) -> None:
        cleaned = redact_pii("Questions? Write to sarah.chen@sdsmanager.com today.")

        assert "sarah.chen@sdsmanager.com" not in cleaned
        assert cleaned == "Questions? Write to [email] today."

    def test_leaves_a_price_alone(self) -> None:
        assert redact_pii("From £49.99 a month.") == "From £49.99 a month."


class TestPhone:
    def test_removes_a_uk_freephone_number(self) -> None:
        cleaned = redact_pii("Call 0800 123 4567 to book a demo.")

        assert "0800 123 4567" not in cleaned
        assert cleaned == "Call [phone] to book a demo."

    def test_removes_an_international_number(self) -> None:
        assert redact_pii("Ring +44 20 7946 0958 now.") == "Ring [phone] now."

    def test_removes_a_us_number_in_parentheses(self) -> None:
        assert redact_pii("Dial (555) 123-4567.") == "Dial [phone]."

    def test_leaves_an_iso_date_alone(self) -> None:
        """3.2.4 binds countdown offers to real dates. Eating one invents a deadline."""
        assert redact_pii("Offer ends 2026-09-30.") == "Offer ends 2026-09-30."

    def test_leaves_a_slashed_date_alone(self) -> None:
        assert redact_pii("Sale ends 30/09/2026.") == "Sale ends 30/09/2026."


class TestPostalAddress:
    def test_removes_a_uk_street_address_and_postcode(self) -> None:
        cleaned = redact_pii("Visit us at 12 Hanover Square, London W1S 1JA.")

        assert "12 Hanover Square" not in cleaned
        assert "W1S 1JA" not in cleaned

    def test_removes_a_us_street_address_and_zip(self) -> None:
        cleaned = redact_pii("Head office: 1600 Amphitheatre Parkway, Mountain View, CA 94043.")

        assert "1600 Amphitheatre Parkway" not in cleaned
        assert "94043" not in cleaned

    def test_leaves_ordinary_marketing_copy_alone(self) -> None:
        """Over-redaction is not free: 3.1.1 quotes this copy back as a do_example."""
        copy = "Rated 5 stars by 200 safety managers. Sign up in 2 minutes."

        assert redact_pii(copy) == copy


class TestPayload:
    def test_redacts_strings_nested_in_a_creative_history_payload(self) -> None:
        payload = {
            "ad_id": "12345",
            "headlines": ["Call 0800 123 4567", "Safety data, sorted"],
            "descriptions": {"long": "Email dan.ops@sdsmanager.com for a quote."},
            "impressions": 4120,
        }

        cleaned = redact_payload(payload)

        assert cleaned["headlines"] == ["Call [phone]", "Safety data, sorted"]
        assert cleaned["descriptions"]["long"] == "Email [email] for a quote."

    def test_leaves_non_string_leaves_untouched(self) -> None:
        """An impression count is not copy. Coercing it to a string loses its type."""
        cleaned = redact_payload({"impressions": 4120, "ctr": 0.031, "enabled": True})

        assert cleaned == {"impressions": 4120, "ctr": 0.031, "enabled": True}

    def test_does_not_mutate_the_row_it_was_given(self) -> None:
        """Evidence stays faithful in the database; only the prompt copy is cleaned."""
        payload = {"headline": "Call 0800 123 4567"}

        redact_payload(payload)

        assert payload["headline"] == "Call 0800 123 4567"

    def test_is_idempotent(self) -> None:
        once = redact_pii("Call 0800 123 4567 or email a@b.com")

        assert redact_pii(once) == once


class TestPromptPayloads:
    """`redact_payload` has to be *used*, or law 30 only covers half the input.

    A node's output becomes the next node's prompt: 3.1.2 and 3.1.3 both read
    3.1.1's payload. Redacting the evidence corpus but handing on a payload
    unredacted would leave a gap exactly one hop wide.
    """

    async def test_an_upstream_payload_is_redacted_before_it_becomes_a_prompt(self) -> None:
        from agent.nodes.content.stage_3_1 import lexicon_rules
        from tests.guideline_support import harness

        h = harness(
            "3.1.2",
            outputs={
                "3.1.1": {
                    "do_examples": [
                        {"text": "Call 0800 123 4567 or email ops@sdsmanager.com", "why": "x"}
                    ]
                }
            },
            answers={"LexiconDraft": {"always": [], "never": [], "case_and_spelling": []}},
        )

        await lexicon_rules.reason(h.ctx, [])
        prompt = h.llm.every_prompt()

        assert "0800 123 4567" not in prompt
        assert "ops@sdsmanager.com" not in prompt
