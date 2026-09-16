#!/usr/bin/env python
"""Dump every public Pydantic model to JSON Schema in `packages/contracts/schemas`.

The frontend consumes these as zod schemas (`pnpm --filter contracts generate`),
so the API's response shapes and the browser's validation cannot drift apart.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "packages" / "contracts" / "schemas"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import BaseModel  # noqa: E402

#: Modules scanned for models. Grows with each phase.
SOURCE_MODULES: tuple[str, ...] = (
    "agent.api.schemas",
    "agent.api.schemas_auth",
    "agent.api.schemas_runs",
    "agent.api.schemas_evidence",
    "agent.api.schemas_approvals",
)


def collect_models() -> dict[str, type[BaseModel]]:
    import importlib

    found: dict[str, type[BaseModel]] = {}
    for module_name in SOURCE_MODULES:
        module = importlib.import_module(module_name)
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                found[obj.__name__] = obj
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    models = collect_models()
    if not models:
        print("no models found", file=sys.stderr)
        return 1

    for name, model in sorted(models.items()):
        schema: dict[str, Any] = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        target = out_dir / f"{name}.json"
        target.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
