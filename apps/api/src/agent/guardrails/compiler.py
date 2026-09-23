"""The only writer of a `RuleSet`, and the only thing allowed to hash one.

Stage 03 PRD §9.1 item 3 and law 26. Compilation is deterministic: the same
guideline payload, the same constants version and the same compiler version
produce byte-identical `compiled` JSON and the same `hash`, in any process, on
any day. A CI test compiles a fixture twice in two subprocesses and asserts the
hashes match, because a hash that drifts makes every audit trail a guess.

Two decisions in here are worth reading before changing anything.

**The hash covers meaning, not timing.** `compiled_at` is excluded from the
hashed material. It has to be: two compiles of an unchanged guideline a minute
apart are the same ruleset, and a hash that said otherwise would mint a new
`ruleset_version` every time anybody pressed the button, quietly stranding
every asset pinned to the old one. Everything that can change a *verdict* is
in the hash — rules, claims, detectors, specs, disclosures, the constants
version and the compiler version, which itself carries the pinned language
library versions.

**Compiling builds the program and throws it away.** Every matcher is prepared
— patterns compiled, term sets stemmed, thresholds checked — purely so that a
rule nobody can evaluate is refused *here*, before it is persisted and made
immutable, rather than at lint time where it would be a runtime error in front
of a writer. The cost is a few milliseconds on an operation that happens once
per publish.

`compiled_at` arrives as an argument. This module may not read a clock.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from agent.guardrails.matchers.lexicon import LANGUAGE_LIB_VERSION
from agent.guardrails.registry import (
    GUARDRAILS_CODE_VERSION,
    GuardrailsError,
    MatcherKind,
    kind_for,
    require_registered,
)
from agent.guidelines.constants import ContentConstants
from agent.schemas.guardrails import (
    AssetSpecSheet,
    ClaimRef,
    DisclosureRule,
    LogoTemplate,
    Rule,
    RuleSet,
)


class CompileError(GuardrailsError):
    """A guideline payload that cannot produce an honest ruleset."""


@dataclass(frozen=True, slots=True)
class PreparedRule:
    """One rule with its matcher already built."""

    rule: Rule
    kind: MatcherKind
    prepared: Any


@dataclass(frozen=True, slots=True)
class Program:
    """A ruleset with every matcher pre-built, ready for the target loop.

    Deliberately not part of `RuleSet`: a compiled pattern has no honest JSON
    rendering, and `RuleSet` is a row in a table that a trigger makes immutable.
    The program is rebuilt when a ruleset is loaded into a process that means to
    lint with it, which is once per `lint()` call and never per target.
    """

    ruleset: RuleSet
    #: Evaluated once per in-scope target.
    per_target: tuple[PreparedRule, ...]
    #: Evaluated once per lint call over every target — see `CountMatcher`.
    per_set: tuple[PreparedRule, ...]


def compiler_version() -> str:
    """`guardrails/1.0+snowball/3.1.1+simplemma/2.0.0`.

    The language libraries are in here because `lemma` resolving `best` to
    `good` is a property of simplemma's dictionary, not of our code. An upgrade
    that changes a lemma changes verdicts, and it must move the hash.
    """
    return f"guardrails/{GUARDRAILS_CODE_VERSION}+{LANGUAGE_LIB_VERSION}"


def build_program(ruleset: RuleSet) -> Program:
    """Pre-build every matcher in a ruleset. Raises on any it cannot."""
    per_target: list[PreparedRule] = []
    per_set: list[PreparedRule] = []
    for rule in ruleset.rules:
        kind = kind_for(rule.matcher)
        try:
            prepared = kind.prepare(rule.matcher)
        except GuardrailsError as exc:
            raise CompileError(f"{rule.rule_id}: {exc}") from exc
        entry = PreparedRule(rule=rule, kind=kind, prepared=prepared)
        (per_set if kind.applies_to == "set" else per_target).append(entry)
    return Program(ruleset=ruleset, per_target=tuple(per_target), per_set=tuple(per_set))


def canonical(value: Any) -> Any:
    """A JSON-safe, stably-ordered rendering. What the hash is taken over.

    Nothing is stripped. Anything omitted here is something a re-compile would
    not notice had changed, and `ruleset_version` is the claim that two
    compilations produced the same rules.
    """
    if isinstance(value, Mapping):
        return {str(key): canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list | tuple):
        return [canonical(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, int | str):
        return value
    if isinstance(value, float):
        # Integral floats collapse to ints so `1.0` and `1` are one value: a
        # ruleset round-trips through JSON and a threshold that came back as a
        # float must not read as a different threshold.
        return int(value) if value.is_integer() else round(value, 10)
    return str(value)


def ruleset_hash(material: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(canonical(material), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _rules_from(payload: Mapping[str, Any]) -> tuple[Rule, ...]:
    raw = payload.get("rules")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise CompileError("guideline payload has no `rules` list")
    rules: list[Rule] = []
    for index, item in enumerate(raw):
        try:
            rules.append(item if isinstance(item, Rule) else Rule.model_validate(item))
        except Exception as exc:  # noqa: BLE001 - re-raised with the index that failed
            raise CompileError(f"rules[{index}] is not a valid Rule: {exc}") from exc
    return tuple(rules)


def _uuid(payload: Mapping[str, Any], key: str) -> UUID:
    value = payload.get(key)
    if value is None:
        raise CompileError(f"guideline payload has no `{key}`")
    return value if isinstance(value, UUID) else UUID(str(value))


def _int(payload: Mapping[str, Any], key: str, default: int) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CompileError(f"`{key}` must be an integer, got {value!r}")
    return value


def _models[ModelT: BaseModel](
    payload: Mapping[str, Any], key: str, model: type[ModelT]
) -> tuple[ModelT, ...]:
    raw = payload.get(key, ())
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise CompileError(f"`{key}` must be a list")
    return tuple(item if isinstance(item, model) else model.model_validate(item) for item in raw)


def _asset_sheet(payload: Mapping[str, Any], constants: ContentConstants) -> AssetSpecSheet:
    """The spec sheet this ruleset enforces: the run's, falling back to the constants'.

    **The run's sheet has to win.** §11 gives 3.4.1 a `scope`: an unbound run
    emits specs for every campaign type, a plan-bound run emits only the
    slate's. Compiling `constants.asset_sheet()` regardless threw that away —
    a scoped run's ruleset carried every campaign type, so Stage 04 linting a
    Search-only account would enforce Performance Max asset counts against it
    and report missing assets for campaigns the plan never intended to run.

    The constants sheet remains the fallback, and it is the right one: it is
    where 3.4.1's numbers come from, so a payload written before this key
    existed compiles to exactly what it compiled to before.
    """
    section = payload.get("asset_specs")
    if isinstance(section, Mapping):
        raw = section.get("sheet", section)
        if isinstance(raw, AssetSpecSheet):
            return raw
        if isinstance(raw, Mapping) and raw.get("specs"):
            try:
                return AssetSpecSheet.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - re-raised with the key that failed
                raise CompileError(f"`asset_specs` is not a valid AssetSpecSheet: {exc}") from exc
    return constants.asset_sheet()


def compile(  # noqa: A001 - `compiler.compile` is the name the PRD gives it
    guideline_payload: Mapping[str, Any],
    constants: ContentConstants,
    claims_index: Sequence[ClaimRef],
    *,
    compiled_at: datetime,
) -> RuleSet:
    """Turn a guideline payload into the machine artifact Stage 04 reads.

    `compiled_at` is keyword-only and required for the same reason `lint`'s
    `now` is: this module may not read a clock, so the caller states the time
    and the result is a function of its arguments.
    """
    rules = _rules_from(guideline_payload)
    require_registered(rules)

    # Sorted by id, then by the rule's own canonical rendering. Two rules can
    # legitimately share an id — a brand can ban two different term sets — so
    # the id alone is not a total order, and an unstable order would change the
    # hash without changing the rules.
    ordered = tuple(
        sorted(
            rules,
            key=lambda rule: (
                rule.rule_id,
                json.dumps(canonical(rule.model_dump(mode="json")), sort_keys=True),
            ),
        )
    )

    claims = tuple(sorted(claims_index, key=lambda claim: str(claim.claim_id)))
    detectors = tuple(sorted(constants.detectors(), key=lambda spec: spec.detector_id))
    disclosures: tuple[DisclosureRule, ...] = _models(
        guideline_payload, "disclosure_requirements", DisclosureRule
    )
    logos: tuple[LogoTemplate, ...] = _models(guideline_payload, "logo_templates", LogoTemplate)

    major = _int(guideline_payload, "version_major", 1)
    minor = _int(guideline_payload, "version_minor", 0)
    version = compiler_version()
    specs = _asset_sheet(guideline_payload, constants)

    material = {
        "schema_version": "1.0",
        "project_id": str(_uuid(guideline_payload, "project_id")),
        "guideline_id": str(_uuid(guideline_payload, "guideline_id")),
        "version_major": major,
        "version_minor": minor,
        "compiler_version": version,
        "constants_version": constants.version,
        "rules": [rule.model_dump(mode="json") for rule in ordered],
        "claims_index": [claim.model_dump(mode="json") for claim in claims],
        "detectors": [spec.model_dump(mode="json") for spec in detectors],
        "asset_specs": specs.model_dump(mode="json"),
        "disclosure_requirements": [item.model_dump(mode="json") for item in disclosures],
        "logo_templates": [item.model_dump(mode="json") for item in logos],
    }
    digest = ruleset_hash(material)

    ruleset = RuleSet(
        ruleset_version=f"{major}.{minor}+{digest[:8]}",
        project_id=_uuid(guideline_payload, "project_id"),
        guideline_id=_uuid(guideline_payload, "guideline_id"),
        compiler_version=version,
        constants_version=constants.version,
        compiled_at=compiled_at,
        rules=ordered,
        claims_index=claims,
        detectors=detectors,
        asset_specs=specs,
        disclosure_requirements=disclosures,
        logo_templates=logos,
        hash=digest,
    )

    # Built and thrown away: a rule nobody can evaluate is refused here, before
    # the row is written and made immutable, rather than at lint time in front
    # of a writer.
    build_program(ruleset)
    return ruleset
