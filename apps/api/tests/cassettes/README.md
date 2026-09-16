# Connector cassettes

Recorded HTTP for the two connectors that talk to a paid API. A test bound to a
cassette replays it with `record_mode="none"`, so an unrecorded request raises
`CannotOverwriteExistingCassetteException` instead of reaching the network —
the suite cannot silently start costing money or start passing because someone
had credentials in their shell.

## Provenance — read this before trusting a shape

These are **hand-authored to the documented response schemas**, not recorded
from live accounts. No Google Ads or DataForSEO credentials existed when P2 was
built, so the payload *shapes* come from each vendor's API reference and the
*values* are representative SDS Manager figures.

What that does and does not buy:

- It does prove the connector's parsing, unit conversion, dedupe keys and
  degradation paths, which is what the tests assert.
- It does **not** prove the field names match production. The first live pull
  is still a real test, and the first thing to suspect if it returns empty
  payloads is a key name in here.

Re-record against a live account as soon as credentials exist:

```bash
# one-off, with real credentials exported
uv run pytest tests/test_google_ads_connector.py --record-mode=once
```

and then scrub the `Authorization`, `developer-token` and refresh-token values
out of the diff before committing. `conftest.py` filters those headers on
record, but the request *bodies* are not filtered.

## Files

| Cassette | Covers |
| --- | --- |
| `google_ads_search.yaml` | OAuth refresh + all five GAQL pulls from PRD §9.1 |
| `dataforseo_demand.yaml` | `keywords_for_site`, `keyword_ideas`, `serp`, `competitors_domain` |
| `dataforseo_task_failure.yaml` | HTTP 200 carrying a failed task — DataForSEO's real failure mode |

`transparency` and `web_crawler` are not cassetted: they parse HTML, so their
fixtures are saved pages under `tests/fixtures/`, which is the same idea in the
format those connectors actually consume.
