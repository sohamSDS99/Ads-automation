"""Build the S4-P2 stub's media catalogue from S4-P1's recorded OpenRouter bodies.

    cd apps/api && uv run python ../web/scripts/s4p2/make_catalogue_fixtures.py

Runs the api's own normalisers (`agent.media.catalogue`) over the fixtures
S4-P1 recorded from the live API, so the records the browser check serves are
exactly the `CapabilityRecord`s `GET /media/catalogue` returns — not a
hand-written guess at them. Re-run it when those fixtures are re-recorded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

API = Path(__file__).resolve().parents[3] / "api"
sys.path.insert(0, str(API / "src"))

from agent.media.catalogue import normalise_image_models, normalise_video_models  # noqa: E402

RECORDED = API / "tests" / "fixtures" / "openrouter"
OUT = Path(__file__).resolve().parent / "fixtures"


def body(name: str) -> dict:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))["body"]


for modality, records in (
    ("image", normalise_image_models(body("images_models.json"))),
    ("video", normalise_video_models(body("videos_models.json"))),
):
    target = OUT / f"{modality}-catalogue.json"
    target.write_text(
        json.dumps([r.model_dump(mode="json") for r in records], indent=1) + "\n", encoding="utf-8"
    )
    print(f"wrote {target.name}: {len(records)} records")
