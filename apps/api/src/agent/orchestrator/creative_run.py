"""What the executor does for a creative run and for no other (Stage 04 PRD §4.3, §8.1).

Two things, both resolved or asserted around the unchanged core loop:

* **`load_resources()`, once, before the first wave.** The `CreativeInput`
  stored on the run (migration 0021) is re-hashed against `Run.input_hash`; the
  project's creative constants must be the version the input names; and the
  run's current pin is loaded through `creative/lint_adapter.py` with the
  input's offer snapshot. Any failure ends the run before a token is spent,
  exactly as a missing `PlanInput` does for a plan run.

* **`assert_node_contract()`, after every creative node's `reason()`.** The two
  `NodeSpec` fields of §8.1 item 2 are only true if something checks them:
  every `GenerationJob` the node left behind is a modality it declared in
  `media`, and every `CreativeAsset` it persisted past `draft` carries a
  passing `LintResult` against the run's current pin (law 33). A node that
  persisted such an asset without declaring `lint_required` fails too — law 33
  says *nothing* is emitted unlinted, not "nothing from the nodes that asked".
"""

from __future__ import annotations

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import lint_adapter
from agent.creative.constants import CreativeConstantsError, creative_constants_for
from agent.db.models import (
    CreativeAsset,
    CreativeAssetStatus,
    GenerationJob,
    Project,
    Run,
)
from agent.nodes.base import CreativeResources, NodeContractError, NodeSpec
from agent.schemas.creative_input import CreativeInput
from agent.schemas.guardrails import LintResult

#: Only these verdicts may leave `draft` (law 33).
PASSING = frozenset({"pass", "pass_with_warnings"})


class CreativeRunError(RuntimeError):
    """A creative run cannot execute. `code` is what the run's error records."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def load_resources(db: AsyncSession, run: Run, project: Project) -> CreativeResources:
    if run.creative_input is None:
        raise CreativeRunError(
            "creative_input_missing",
            f"Run {run.id} carries no CreativeInput. It was started before migration 0021 "
            "or outside POST /projects/{id}/creative/runs; start a new creative run.",
        )
    try:
        built = CreativeInput.model_validate(run.creative_input)
    except ValidationError as exc:
        raise CreativeRunError(
            "creative_input_invalid", f"Run {run.id}'s CreativeInput does not validate: {exc}"
        ) from exc
    digest = built.content_hash()
    if digest != run.input_hash:
        raise CreativeRunError(
            "creative_input_tampered",
            f"Run {run.id}'s CreativeInput hashes to {digest}, not the {run.input_hash} it "
            "was started with. The input is immutable once a run starts.",
        )
    if built.creative_run_id != run.id:
        raise CreativeRunError(
            "creative_input_foreign",
            f"Run {run.id} carries the CreativeInput of run {built.creative_run_id}.",
        )
    try:
        constants = creative_constants_for(project)
    except CreativeConstantsError as exc:
        raise CreativeRunError("creative_constants", str(exc)) from exc
    if constants.version != built.constants_version:
        raise CreativeRunError(
            "creative_constants_changed",
            f"Run {run.id} was started under creative constants {built.constants_version}; "
            f"this project now resolves {constants.version}. Start a new run to use them.",
        )
    try:
        pin = lint_adapter.current_pin(run)
        linter = await lint_adapter.load(
            db,
            workspace_id=run.workspace_id,
            pin=pin,
            offer_records=built.offer_records,
            expected_hash=(
                built.ruleset_ref.hash if pin == built.ruleset_ref.ruleset_version else None
            ),
        )
    except lint_adapter.LintAdapterError as exc:
        raise CreativeRunError("ruleset_pin", str(exc)) from exc
    return CreativeResources(input=built, linter=linter, constants=constants)


async def assert_node_contract(db: AsyncSession, run: Run, spec: NodeSpec) -> None:
    """§8.1 item 2 for one node, after `reason()`. Raises `NodeContractError`."""
    await db.flush()
    modalities = (
        (
            await db.execute(
                sa.select(GenerationJob.modality)
                .where(GenerationJob.creative_run_id == run.id, GenerationJob.node_id == spec.id)
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    undeclared = sorted({str(m.value) for m in modalities} - set(spec.media))
    if undeclared:
        raise NodeContractError(
            f"node {spec.id} submitted {', '.join(undeclared)} generation but declares "
            f"media={list(spec.media)}. Declare the modality or do not submit it."
        )

    assets = (
        (
            await db.execute(
                sa.select(CreativeAsset).where(
                    CreativeAsset.creative_run_id == run.id,
                    CreativeAsset.node_id == spec.id,
                    CreativeAsset.status != CreativeAssetStatus.DRAFT,
                )
            )
        )
        .scalars()
        .all()
    )
    if not assets:
        return
    if not spec.lint_required:
        raise NodeContractError(
            f"node {spec.id} persisted {len(assets)} asset(s) past draft but does not declare "
            "lint_required. Law 33: nothing is emitted unlinted."
        )
    pin = lint_adapter.current_pin(run)
    unlinted = sorted(str(asset.id) for asset in assets if not _linted(asset, pin))
    if unlinted:
        raise NodeContractError(
            f"node {spec.id} left {len(unlinted)} asset(s) past draft without a passing "
            f"LintResult against the current pin {pin}: {', '.join(unlinted)}"
        )


def _linted(asset: CreativeAsset, pin: str) -> bool:
    if asset.lint is None or asset.ruleset_version != pin:
        return False
    try:
        result = LintResult.model_validate(asset.lint)
    except ValidationError:
        return False
    return result.ruleset_version == pin and result.verdict in PASSING
