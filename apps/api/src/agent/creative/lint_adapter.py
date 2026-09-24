"""The only Stage 04 caller of `guardrails.linter.lint()` (PRD §4.3 rule 2, law 33).

Stage 04 never evaluates a rule itself. Every text candidate and every image
rendition is checked here, against **the run's current pin**, with the
`CreativeInput.offer_records` snapshot and a `now` the caller passes — exactly
the three arguments Stage 03 §9.1 says a verdict may depend on. The linter
never receives `CreativeInput`; it receives a `RuleSet` loaded by pin.

Why one door rather than a helper every node calls `lint()` through: a second
caller is a second place to forget the offers (so a stale price passes), to
read a clock instead of taking `now` (so two runs disagree), or to lint against
whichever ruleset is newest instead of the pinned one (so an asset produced
under v1 is judged by v2's rules). `tests/creative/test_lint_adapter.py` fails
if any other Stage 04 module imports the linter.

A pin is resolved exactly as `GET /guidelines/published/ruleset/{version}`
resolves it — by `ruleset_version` within the workspace — so a superseded pin
still loads and still returns the verdicts it always returned.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import RuleSet as RuleSetRow
from agent.db.models import Run
from agent.guardrails.compiler import Program, build_program
from agent.guardrails.linter import lint as guardrails_lint
from agent.schemas.guardrails import LintResult, LintTarget, OfferRecord, RuleSet


class LintAdapterError(RuntimeError):
    """The run's pin cannot be resolved to a usable ruleset."""


def current_pin(run: Run) -> str:
    """The `ruleset_version` a creative run lints against right now.

    `Run.pins` is append-only (PRD §4.4): the first entry is `reason='start'`
    and the only other reason is `h3_clearance`. The last entry wins.
    """
    pins = run.pins or []
    if not pins:
        raise LintAdapterError(
            f"run {run.id} has no ruleset pin, so there are no rules to lint against"
        )
    version = pins[-1].get("ruleset_version") if isinstance(pins[-1], dict) else None
    if not version:
        raise LintAdapterError(f"run {run.id}'s latest pin names no ruleset_version")
    return str(version)


@dataclass(frozen=True)
class PinnedLinter:
    """One pinned ruleset and one offer snapshot. `lint()` takes only `now`."""

    ruleset: RuleSet
    offer_records: tuple[OfferRecord, ...]
    #: Built once per pin, not per call: a node linting 25 candidates builds
    #: the matchers once, which is what `lint(program=)` exists for.
    _program: Program = field(init=False, repr=False, compare=False)
    #: The same program without its set rules — see `lint_candidate`.
    _candidate_program: Program = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        program = build_program(self.ruleset)
        object.__setattr__(self, "_program", program)
        object.__setattr__(self, "_candidate_program", replace(program, per_set=()))

    @property
    def pin(self) -> str:
        return self.ruleset.ruleset_version

    def lint(self, targets: Sequence[LintTarget], *, now: datetime) -> LintResult:
        return guardrails_lint(
            targets,
            self.ruleset,
            now=now,
            offers=self.offer_records,
            program=self._program,
        )

    def lint_candidate(self, target: LintTarget, *, now: datetime) -> LintResult:
        """One candidate, linted alone at creation (law 33), against every
        per-target rule its scope selects.

        The set rules — the spec sheet's asset counts — are left out, and only
        they are: "3 to 15 headlines" is a property of the assembled ad, not of
        one headline, so asking a lone candidate would fail every candidate on
        "there is 1 of headline". The ad is counted where it is assembled. The
        linter still evaluates every rule it is given; this chooses which
        compiled rules a candidate is subject to, and implements none.
        """
        return guardrails_lint(
            [target],
            self.ruleset,
            now=now,
            offers=self.offer_records,
            program=self._candidate_program,
        )


async def load(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    pin: str,
    offer_records: Sequence[OfferRecord] = (),
    expected_hash: str | None = None,
) -> PinnedLinter:
    """The ruleset `pin` names, ready to lint. Raises `LintAdapterError`.

    `expected_hash` is `CreativeInput.ruleset_ref.hash` when `pin` is the start
    pin: the row that answers to that version must be the ruleset the input
    was hashed against, byte for byte, or the run is linting against rules it
    never pinned.
    """
    row = (
        await db.execute(
            sa.select(RuleSetRow).where(
                RuleSetRow.workspace_id == workspace_id, RuleSetRow.ruleset_version == pin
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise LintAdapterError(f"no ruleset {pin!r} in this workspace")
    if expected_hash is not None and row.hash != expected_hash:
        raise LintAdapterError(
            f"ruleset {pin!r} hashes to {row.hash}, but the run pinned {expected_hash}"
        )
    try:
        ruleset = RuleSet.model_validate(row.compiled)
    except ValidationError as exc:
        raise LintAdapterError(f"ruleset {pin!r} is not a readable RuleSet: {exc}") from exc
    if ruleset.ruleset_version != pin:
        raise LintAdapterError(
            f"ruleset row {pin!r} carries a compiled ruleset for {ruleset.ruleset_version!r}"
        )
    return PinnedLinter(ruleset=ruleset, offer_records=tuple(offer_records))
