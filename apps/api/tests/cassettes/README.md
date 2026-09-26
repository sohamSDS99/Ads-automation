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

## Stage 04 golden runs — `creative/`

`creative/<fixture>.json` is every provider response one golden creative run
consumed (PRD §17 CC9, CC16): chat completions, image generations, video
submits, polls and downloads. They are **not** vcrpy tapes. They are read by
`tests/integration/creative_cassette.py`, which keys each interaction by
*what was asked* rather than by position. That matters because a wave of
independent nodes calls the model concurrently.

- The key is the SHA-256 of the request's canonical JSON, with UUIDs,
  timestamps, dates and 64-hex digests normalised away.
- A request whose normalised form was never recorded is a **miss**: the node
  fails and names the first place the live request differs from the recording.
- A recorded interaction the run never asked for is **unplayed**, and the
  golden test fails on it.
- Photographs are stored as their recipe (`golden_creative.photo(size, seed)`)
  plus the SHA-256 of the exact bytes. Clips are stored as the path of a
  committed fixture file plus its SHA-256.

A miss after a deliberate prompt or schema change means the cassette is stale.
Re-record all three fixtures from the scripted sources in
`tests/integration/golden_creative.py` (run inside the worker test image):

    GOLDEN_RECORD=1 pytest tests/integration/test_s4p24_golden.py

Then replay twice without `GOLDEN_RECORD`. Both runs must pass before the new
cassettes are committed.

| Cassette | Fixture |
| --- | --- |
| `creative/search_lead_gen.json` | Search only, lead-gen: A/B RSAs, extras, lead form |
| `creative/search_pmax_ecommerce.json` | Search + PMax shop: bound offers, asset-group text, images through G8 |
| `creative/full_slate_video.json` | Search + PMax with images and video: every media node, lead form, offers |
