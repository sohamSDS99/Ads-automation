"""4.7.2 `creative_critique` — the blocking checklist, then a reader (Stage 04 PRD §11 4.7.2).

`issues[]{severity, section, finding, fix}`. **The thirteen blocking checks are
code** (`creative/checklist.py`), run over the package 4.7.1 assembled, at the
moment this node runs. **The CRITIQUE model only reads**: it is shown the
approved brief, the copy that ships and what the checks found, and may add
brief-adherence findings at `warning` or `note` — its output schema has no
`blocking`, so it cannot block a package, and it cannot clear one either.

Any blocking issue ⇒ the package is `blocked`; none ⇒ `ready_to_release`. The
status is written to the row and to its payload (where `package_hash` does not
cover it). 4.7.1 is not re-run: it is deterministic, so re-running it would
change nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from agent.creative import checklist
from agent.creative import package as packages
from agent.db.models import CreativeBrief, CreativePackageStatus, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes import prompts
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative.n4_6_4_final_lint_and_render import final_linter
from agent.schemas.creative_package import (
    CreativeCritique,
    CreativePackage,
    CritiqueIssue,
    PackageAssembly,
)

NODE_ID = "4.7.2"
#: How much of the brief and the copy the reader is shown.
BRIEF_CHARS = 6_000
COPY_CHARS = 12_000
STATUS = {
    "blocked": CreativePackageStatus.BLOCKED,
    "ready_to_release": CreativePackageStatus.READY_TO_RELEASE,
}


class ReadingIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Never `blocking`: the thirteen checks are the only thing that blocks.
    severity: Literal["warning", "note"]
    section: str = Field(min_length=1)
    finding: str = Field(min_length=1)
    fix: str


class CreativeCritiqueDraft(BaseModel):
    """What a reader sees that no predicate can: the package against its brief."""

    model_config = ConfigDict(extra="forbid")

    issues: list[ReadingIssue] = Field(default_factory=list)


class CreativeCritiqueNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="creative_critique",
        stage="4.7",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.7.1",),
        task_class=TaskClass.CRITIQUE,
        input_model=PackageAssembly,
        output_model=CreativeCritique,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        row = await packages.package_row(ctx.db, ctx.run.id, lock=True)
        if row is None:  # pragma: no cover — 4.7.1 is a hard dependency
            raise NodeContractError("4.7.2 ran before 4.7.1 wrote a package")
        package = CreativePackage.model_validate(row.payload)
        linter = await final_linter(ctx, creative.linter)
        context = await checklist.read_context(
            ctx.db,
            package=package,
            project=ctx.project,
            ruleset=linter.ruleset,
            constants=creative.constants,
            outputs=ctx.outputs,
            now=datetime.now(UTC),
        )
        asserted = checklist.run_checks(context)
        reading = await self._read(ctx, package, asserted)
        issues = [*asserted, *reading]
        status = checklist.status_for(issues)

        row.status = STATUS[status]
        row.payload = {**row.payload, "status": status}
        await ctx.db.flush()

        blocking = checklist.blocking(issues)
        warnings = [issue for issue in issues if issue.severity == "warning"]
        notes = [issue for issue in issues if issue.severity == "note"]
        await ctx.progress(
            f"package {status.replace('_', ' ')} — {len(blocking)} blocking, "
            f"{len(warnings)} warning(s), {len(notes)} note(s)"
        )
        return CreativeCritique(
            package_id=package.package_id,
            status=status,
            issues=issues,
            blocking=len(blocking),
            warnings=len(warnings),
            notes=len(notes),
            failed_checks=sorted({issue.check for issue in blocking}, key=_check_order),
        )

    async def _read(
        self, ctx: RunContext, package: CreativePackage, asserted: list[CritiqueIssue]
    ) -> list[CritiqueIssue]:
        brief = await _brief_markdown(ctx)
        reading = await ctx.complete(
            CreativeCritiqueDraft,
            system=prompts.system_prompt(
                "You are reading an ad package against the one-page brief it was written "
                "from, for the approver who is about to release it. Thirteen mechanical "
                "checks have already run and their results are shown — do not repeat or "
                "re-derive them, and do not judge lengths, counts, claims, offers, links or "
                "media specs. Report only where the copy strays from the brief: an ad group "
                "whose copy does not carry its primary message, a variant B that does not "
                "test its stated angle, copy that talks to an audience the brief excludes, "
                "a non-negotiable voice word or term ignored. Each finding names the section "
                "(an ad_ref or an asset id) and a fix. Severity is `warning` when it would "
                "cost the campaign, `note` otherwise. No finding is better than an invented one."
            ),
            user=prompts.compose(
                prompts.computed_block(
                    "the thirteen checks have already reported",
                    [issue.model_dump(mode="json") for issue in asserted[:40]],
                ),
                f"THE APPROVED BRIEF\n{brief[:BRIEF_CHARS]}",
                f"THE COPY THAT SHIPS\n{_copy(package)[:COPY_CHARS]}",
            ),
            task_class=TaskClass.CRITIQUE,
        )
        return [
            CritiqueIssue(
                severity=item.severity,
                section=item.section,
                finding=item.finding,
                fix=item.fix,
                check="reader",
            )
            for item in reading.issues
        ]


async def _brief_markdown(ctx: RunContext) -> str:
    row = (
        await ctx.db.execute(
            sa.select(CreativeBrief).where(CreativeBrief.creative_run_id == ctx.run.id)
        )
    ).scalar_one_or_none()
    return row.markdown if row is not None else ""


def _copy(package: CreativePackage) -> str:
    """Every ad and extra as text, by campaign — what the reader reads."""
    lines: list[dict[str, object]] = []
    for campaign in package.campaigns:
        texts = {asset.asset_id: asset for asset in campaign.text_assets}
        for ad in campaign.ads:
            lines.append(
                {
                    "ad_ref": ad.ad_ref,
                    "angle": ad.angle,
                    "hypothesis": ad.hypothesis,
                    "headlines": [texts[a].text for a in ad.headlines if a in texts],
                    "descriptions": [texts[a].text for a in ad.descriptions if a in texts],
                }
            )
        extras = [
            {"asset_id": str(asset.asset_id), "kind": asset.kind, "text": asset.text}
            for asset in campaign.text_assets
            if asset.surface not in ("rsa_headline", "rsa_description", "rsa_path")
        ]
        if extras:
            lines.append({"campaign_ref": campaign.campaign_ref, "extras": extras})
    return json.dumps(lines, ensure_ascii=False, indent=1)


def _check_order(check: str) -> tuple[int, str]:
    number = check.removeprefix("check_")
    return (int(number), "") if number.isdigit() else (99, check)


CREATIVE_CRITIQUE = CreativeCritiqueNode()
