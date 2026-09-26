"""4.7.1 `package_assembly` — the `CreativePackage` (Stage 04 PRD §11 4.7.1, §12.3).

**Deterministic code, no LLM.** The node reads the run's rows and its nodes'
outputs once (`package.read_snapshot`), assembles the package
(`package.assemble` — pure, sorted, hashed) and writes it as the run's one
`CreativePackage` row: status `draft`, version 0 (release mints one), the
payload with its manifest and `package_hash`. The row's own `manifest` and
`package_hash` columns stay empty until release writes the files they
describe. 4.7.2 decides `blocked` or `ready_to_release`.

A retry rewrites the draft in place — same row, same id. A package that has
been released is never touched again: it is immutable (law 42, trigger
`creative_package_released`), and a changed package is a new run.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from agent.creative import package as packages
from agent.creative.package import AssemblyError
from agent.db.models import CreativePackage as CreativePackageRow
from agent.db.models import CreativePackageStatus, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative.n4_6_4_final_lint_and_render import final_linter
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_package import PACKAGE_SCHEMA_VERSION, PackageAssembly

NODE_ID = "4.7.1"
#: Everything before it — §11's `4.7.1←{all}`.
UPSTREAM = (
    "4.1.1", "4.2.1", "4.2.2", "4.2.3", "4.2.4", "4.2.5", "4.5.1", "4.5.2", "4.3.1",
    "4.3.2", "4.3.3", "4.4.1", "4.4.2", "4.4.3", "4.4.4", "4.4.5", "4.4.6", "4.4.7",
    "4.6.1", "4.6.2", "4.6.3", "4.6.4",
)  # fmt: skip
UNRELEASED = frozenset(
    {
        CreativePackageStatus.DRAFT,
        CreativePackageStatus.BLOCKED,
        CreativePackageStatus.READY_TO_RELEASE,
    }
)


class PackageAssemblyNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="package_assembly",
        stage="4.7",
        run_stage=RunStage.CREATIVE,
        depends_on=UPSTREAM,
        task_class=TaskClass.CLASSIFY,
        input_model=CreativeInput,
        output_model=PackageAssembly,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        linter = await final_linter(ctx, creative.linter)
        await ctx.db.refresh(ctx.run, attribute_names=["cost_usd"])
        row = await packages.package_row(ctx.db, ctx.run.id, lock=True)
        if row is not None and row.status not in UNRELEASED:
            raise NodeContractError(
                f"package {row.id} of this run is {row.status.value}: a released package is "
                "immutable, and a changed one is a new run"
            )
        snap = await packages.read_snapshot(
            ctx.db,
            ctx.run,
            inp=creative.input,
            ruleset=linter.ruleset,
            constants=creative.constants,
            outputs=ctx.outputs,
            package_id=row.id if row is not None else uuid.uuid4(),
            version=0,
            status="draft",
        )
        try:
            built, _ = packages.assemble(snap)
        except AssemblyError as exc:
            raise NodeContractError(f"4.7.1 cannot assemble the package: {exc}") from exc

        if row is None:
            row = CreativePackageRow(
                id=built.package_id,
                workspace_id=ctx.run.workspace_id,
                project_id=ctx.run.project_id,
                creative_run_id=ctx.run.id,
            )
            ctx.db.add(row)
        row.schema_version = PACKAGE_SCHEMA_VERSION
        row.version = 0
        row.status = CreativePackageStatus.DRAFT
        row.plan_id = built.pins.plan_id
        row.plan_version = built.pins.plan_version
        row.guideline_id = linter.ruleset.guideline_id
        row.ruleset_version = built.pins.ruleset_version
        row.brief_hash = built.brief_hash or None
        row.payload = built.model_dump(mode="json")
        row.manifest = None
        row.package_hash = None
        row.cost_usd = built.cost.total_usd
        await ctx.db.flush()

        assets = sum(len(c.text_assets) + len(c.media) + len(c.logos) for c in built.campaigns)
        await ctx.progress(
            f"package assembled: {len(built.campaigns)} campaign(s), {assets} asset(s), "
            f"{len(built.manifest)} file(s) at {built.pins.ruleset_version}"
        )
        return PackageAssembly(
            package_id=built.package_id,
            version=0,
            status="draft",
            ruleset_version=built.pins.ruleset_version,
            campaigns=len(built.campaigns),
            assets=assets,
            manifest=built.manifest,
            launch_minimums=[c.launch_minimums for c in built.campaigns],
            package_hash=built.package_hash,
        )


PACKAGE_ASSEMBLY = PackageAssemblyNode()
