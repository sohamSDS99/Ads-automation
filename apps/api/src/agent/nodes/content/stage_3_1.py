"""Stage 3.1 — brand rules. Nodes 3.1.1, 3.1.2 and 3.1.3 (⛳ G5).

Two invariants carry this stage, and neither of them can be a sentence in a
prompt, because a model that is told not to do something still sometimes does it.

**3.1.1 quotes; it never writes.** §11 says the examples are "quoted from real
best-performing copy, never invented". So every example the model returns is
checked against the corpus the node actually gathered, and one that is not
there fails the node. The single exception is `rewritten_as` — the "say this
instead" half of a don't-example is the one field the model is supposed to
author.

**3.1.2 emits only what compiles.** §11 says the lexicon is
"machine-checkable: every entry compiles to a matcher". An entry that cannot
become a `TermSetMatcher` enforces nothing at lint time, so publishing it would
put a rule in the rulebook that silently passes everything. Those entries move
into `conflicts[]` with a reason instead.

**3.1.3 states its confidence.** When the brand book could not be read, the
rules come from live creative and `extraction_confidence` says `low` — so the
brand owner answering G5 knows what they are confirming (§10.2).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.db.models import ApprovalRequiredRole, Evidence, RunStage
from agent.evidence.redact import redact_pii
from agent.guardrails.registry import kind_for
from agent.llm.router import TaskClass
from agent.nodes import gather, prompts
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.schemas.guardrails import Matcher, TermSetMatcher

#: The gate key 3.1.3 writes onto.
VISUAL_GATE = "G5"

#: Evidence kinds 3.1.1 profiles voice from, best-performing copy first.
VOICE_KINDS = ("creative_history", "site_pages", "brand_book_span")

#: Performance labels Google reports, best first. A `BEST` asset is what §11
#: means by "real best-performing copy"; the order is what decides which
#: examples are put in front of the model first.
LABEL_RANK = {"BEST": 0, "GOOD": 1, "LOW": 2, "LEARNING": 3, "PENDING": 4, "UNKNOWN": 5}


# ---------------------------------------------------------------------------
# 3.1.1 voice_profile
# ---------------------------------------------------------------------------


class DoExample(BaseModel):
    text: str = Field(min_length=1)
    source_ref: str = ""
    why: str = ""


class DontExample(BaseModel):
    text: str = Field(min_length=1)
    #: The one field the model authors rather than quotes.
    rewritten_as: str = ""
    why: str = ""


class Register(BaseModel):
    formality: str = ""
    person: str = ""
    tense: str = ""


class ReadabilityTargets(BaseModel):
    max_sentence_words: int | None = None
    max_syllables_per_word: int | None = None


class VoiceDraft(BaseModel):
    """What the model is asked for.

    `voice_register` is spelled `register` on the wire, which is the name §11
    gives it and therefore the key the rulebook, the exports and the diff all
    use. It cannot be the Python attribute name too: pydantic's metaclass
    inherits `ABCMeta.register`, so a field called `register` shadows it and
    warns on every import. The alias keeps the contract and the rename keeps
    the suite output clean.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    voice_words: list[str] = Field(min_length=3, max_length=5)
    definition_per_word: dict[str, str] = Field(default_factory=dict)
    do_examples: list[DoExample] = Field(default_factory=list)
    dont_examples: list[DontExample] = Field(default_factory=list)
    voice_register: Register = Field(default=Register(), alias="register")
    readability_targets: ReadabilityTargets = ReadabilityTargets()


class VoiceProfileOutput(VoiceDraft):
    """3.1.1 — how this brand sounds, evidenced by what it has already said."""

    #: `bound` when a Google Ads creative history was available, `unbound` when
    #: voice had to be profiled from site copy alone (§10.3). Carried so the
    #: rulebook can say so on its face rather than reading identically in both
    #: cases.
    input_mode: Literal["bound", "unbound"] = "unbound"


class VoiceProfileNode:
    """3.1.1 — profiles voice from copy that already exists."""

    spec = NodeSpec(
        id="3.1.1",
        name="voice_profile",
        stage="3.1",
        run_stage=RunStage.GUIDELINE,
        depends_on=(),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=VoiceProfileOutput,
        connectors=("google_ads", "web_crawler", "brand_book"),
    )

    def needs(self, domain: str | None = None) -> tuple[gather.Need, ...]:
        """Every source optional, and that is the design (law 21, §10.3).

        A project with no connected ad account and no parseable brand book is
        the *first-class* path for this stage, not a degraded one. Marking any
        of these required would put a `creative_history: unavailable` coverage
        note on a rulebook that is perfectly well evidenced from site copy.
        """
        return (
            gather.Need("creative_history", connector="google_ads", optional=True),
            gather.Need(
                "site_pages",
                connector="web_crawler",
                params={"domain": domain},
                optional=True,
            ),
            gather.Need("brand_book_span", connector="brand_book", optional=True),
        )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(ctx, *self.needs(ctx.project.domain))
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        corpus = [row for row in ev if row.kind in VOICE_KINDS]
        creative = [row for row in corpus if row.kind == "creative_history"]

        draft = await ctx.complete(
            VoiceDraft,
            system=prompts.system_prompt(
                "You describe how a brand already sounds. Every example you give is "
                "copied word for word from the copy you are shown — you never write a "
                "new example, and you never improve one. The only thing you write "
                "yourself is the replacement in a don't-example."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "copy this brand has already published, best-performing first",
                    _corpus_block(corpus),
                ),
            ),
        )

        _assert_quoted(draft, corpus)
        return VoiceProfileOutput(
            **draft.model_dump(),
            input_mode="bound" if creative else "unbound",
        )


def _corpus_block(rows: list[Evidence]) -> list[dict[str, Any]]:
    """The copy the model reads: ranked, redacted, and nothing else.

    Redacted here rather than at the connector, because `Evidence` has to stay
    faithful to what a site really says — 3.2.1 harvests claims from these same
    rows. Only the rendering handed to a model is cleaned (law 30).
    """
    ranked = sorted(rows, key=_rank)
    return [
        {
            "kind": row.kind,
            "text": redact_pii(row.content_text or ""),
            "performance": (row.payload or {}).get("performance_label"),
        }
        for row in ranked
    ]


def _rank(row: Evidence) -> tuple[int, int]:
    label = str((row.payload or {}).get("performance_label") or "UNKNOWN").upper()
    kind_order = VOICE_KINDS.index(row.kind) if row.kind in VOICE_KINDS else len(VOICE_KINDS)
    return (kind_order, LABEL_RANK.get(label, len(LABEL_RANK)))


def _assert_quoted(draft: VoiceDraft, corpus: list[Evidence]) -> None:
    """Every example is real copy (§11).

    Compared against the *unredacted* corpus: the model was shown the redacted
    rendering, so a quote it returns is a substring of that, and holding it to
    the original would fail honest quotes of a line that happened to contain a
    phone number. Normalised on whitespace only — a quote that differs by a
    word is not a quote.
    """
    haystack = "  ".join(" ".join((row.content_text or "").split()) for row in corpus)
    redacted = "  ".join(" ".join(redact_pii(row.content_text or "").split()) for row in corpus)
    quoted: list[DoExample | DontExample] = [*draft.do_examples, *draft.dont_examples]
    for example in quoted:
        needle = " ".join(example.text.split())
        if needle and needle not in haystack and needle not in redacted:
            raise NodeContractError(
                f"3.1.1 returned an example that is not quoted from the copy it was shown: "
                f"{example.text!r}. §11 requires examples to come from real "
                "best-performing copy, and an invented one would teach the rulebook a "
                "voice this brand has never used."
            )


# ---------------------------------------------------------------------------
# 3.1.2 lexicon_rules
# ---------------------------------------------------------------------------


class LexiconEntry(BaseModel):
    """One vocabulary rule. `always` and `never` differ only in polarity."""

    term: str = Field(min_length=1)
    surface_forms: list[str] = Field(default_factory=list)
    locale: str = "en"
    severity: Literal["blocking", "warning", "advisory"] = "warning"
    #: `always` entries carry a context, `never` entries a reason. Both optional
    #: so one model can serve both halves.
    context: str = ""
    reason: str = ""
    suggested_replacement: str = ""


class CaseRule(BaseModel):
    canonical: str = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)


class LexiconConflict(BaseModel):
    term: str
    why: str


class LexiconDraft(BaseModel):
    always: list[LexiconEntry] = Field(default_factory=list)
    never: list[LexiconEntry] = Field(default_factory=list)
    case_and_spelling: list[CaseRule] = Field(default_factory=list)


class LexiconOutput(LexiconDraft):
    """3.1.2 — the vocabulary, with everything unenforceable moved aside."""

    conflicts: list[LexiconConflict] = Field(default_factory=list)


class LexiconRulesNode:
    """3.1.2 — vocabulary that compiles, or does not ship as vocabulary."""

    spec = NodeSpec(
        id="3.1.2",
        name="lexicon_rules",
        stage="3.1",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.1.1",),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=LexiconOutput,
        connectors=("brand_book", "google_ads"),
    )

    def needs(self) -> tuple[gather.Need, ...]:
        return (
            gather.Need("brand_book_span", connector="brand_book", optional=True),
            gather.Need("creative_history", connector="google_ads", optional=True),
        )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(ctx, *self.needs())
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        voice = ctx.outputs.get("3.1.1") or {}
        draft = await ctx.complete(
            LexiconDraft,
            system=prompts.system_prompt(
                "You write a brand's vocabulary rules: the words that must appear, the "
                "words that must never appear, and the spellings that are canonical. "
                "Every entry has to be checkable by a machine against a piece of ad "
                "copy — if you cannot say which exact words to look for, do not write "
                "the entry."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block("the voice profile this lexicon serves", voice),
                prompts.computed_block(
                    "regulated terms legal has already ruled on (may be empty)",
                    (ctx.outputs.get("1.1.5") or {}).get("regulated_terms") or [],
                ),
            ),
        )
        return self._sift(draft)

    def _sift(self, draft: LexiconDraft) -> LexiconOutput:
        """Keep what compiles and what does not contradict itself.

        Order matters: contradictions are found first, because a term that is
        both required and banned is a *pair* of entries to withdraw, and
        discovering that after one of them had already been dropped for not
        compiling would leave the other standing.
        """
        conflicts: list[LexiconConflict] = []
        clashing = _both_ways(draft.always, draft.never)
        # Back to the spelling a person wrote. `clashing` holds case-folded keys
        # so that "Value" and "value" collide, but a conflict card that said
        # `('value', 'en')` would be reporting this function's bookkeeping
        # rather than the term somebody has to go and fix.
        spelling = {_key(entry): entry.term for entry in [*draft.never, *draft.always]}
        for key in sorted(clashing):
            conflicts.append(
                LexiconConflict(
                    term=spelling.get(key, key[0]),
                    why=(
                        "the same term is listed as always and as never in the same locale, "
                        "so the linter would both require and forbid it on one piece of copy"
                    ),
                )
            )

        always: list[LexiconEntry] = []
        never: list[LexiconEntry] = []
        for entries, mode, keep in (
            (draft.always, "require", always),
            (draft.never, "forbid", never),
        ):
            for entry in entries:
                if _key(entry) in clashing:
                    continue
                try:
                    self.matcher_for(entry, mode=mode)  # type: ignore[arg-type]
                except (ValueError, Exception) as exc:  # noqa: BLE001 — recorded, not raised
                    conflicts.append(
                        LexiconConflict(
                            term=entry.term,
                            why=f"the entry does not compile to a matcher: {exc}",
                        )
                    )
                    continue
                keep.append(entry)

        return LexiconOutput(
            always=always,
            never=never,
            case_and_spelling=draft.case_and_spelling,
            conflicts=conflicts,
        )

    def matcher_for(self, entry: LexiconEntry, *, mode: Literal["forbid", "require"]) -> Matcher:
        """The `TermSetMatcher` one lexicon entry becomes.

        The §21 exit criterion — "every lexicon entry compiles to a matcher" —
        is this function not raising. It goes through the guardrails registry
        rather than constructing the matcher and hoping, because a matcher kind
        with no registered evaluator compiles cleanly, evaluates nothing, and
        reads as a rule that passes everything.
        """
        terms = [form.strip() for form in [entry.term, *entry.surface_forms] if form.strip()]
        if not terms:
            raise ValueError("no usable surface form")
        matcher = TermSetMatcher(
            terms=tuple(dict.fromkeys(terms)),
            mode=mode,
            match="lemma",
            locale=entry.locale or "en",
        )
        kind_for(matcher).prepare(matcher)
        return matcher


def _key(entry: LexiconEntry) -> tuple[str, str]:
    return (entry.term.strip().casefold(), (entry.locale or "en").casefold())


def _both_ways(always: list[LexiconEntry], never: list[LexiconEntry]) -> set[tuple[str, str]]:
    return {_key(entry) for entry in always} & {_key(entry) for entry in never}


#: Module-level instances. The registry discovers instances, not classes.
voice_profile = VoiceProfileNode()
lexicon_rules = LexiconRulesNode()


# ---------------------------------------------------------------------------
# 3.1.3 visual_identity_rules — G5
# ---------------------------------------------------------------------------


class LogoRules(BaseModel):
    """`clear_space_ratio` and `min_width_px` are measurements, not opinions.

    Both are optional and stay `None` when the brand book did not state them.
    A model-invented ratio is worse than a missing one: it reads like something
    that was measured, and a designer would follow it.
    """

    assets: list[str] = Field(default_factory=list)
    clear_space_ratio: float | None = None
    min_width_px: int | None = None
    permitted_variants: list[str] = Field(default_factory=list)
    forbidden_treatments: list[str] = Field(default_factory=list)


class ColourToken(BaseModel):
    name: str = ""
    hex: str = Field(min_length=4)
    role: str = ""


class ColourRules(BaseModel):
    tokens: list[ColourToken] = Field(default_factory=list)
    pairs_meeting_contrast: list[str] = Field(default_factory=list)
    forbidden_pairs: list[str] = Field(default_factory=list)


class ImageryRules(BaseModel):
    permitted_subjects: list[str] = Field(default_factory=list)
    forbidden_subjects: list[str] = Field(default_factory=list)
    treatment_notes: list[str] = Field(default_factory=list)
    stock_policy: str = ""


class VisualDraft(BaseModel):
    logo: LogoRules = LogoRules()
    colour: ColourRules = ColourRules()
    imagery: ImageryRules = ImageryRules()


class VisualIdentityOutput(VisualDraft):
    """3.1.3 ⛳ G5 — what the brand looks like, and how sure we are."""

    extraction_confidence: Literal["high", "medium", "low"] = "low"
    #: Rendered on the G5 card. §10.2 requires the brand owner to be told
    #: plainly when the rules were inferred from live creative rather than read
    #: out of their brand book — otherwise they confirm a guess believing they
    #: are confirming their own document.
    derivation_note: str = ""
    #: Colours the model named that no extractor ever sampled. Reported rather
    #: than silently dropped, so the gate card can show what was discarded.
    dropped_colours: list[str] = Field(default_factory=list)


class VisualIdentityNode:
    """3.1.3 ⛳ G5 — routed to the brand owner named by 3.5.1."""

    spec = NodeSpec(
        id="3.1.3",
        name="visual_identity_rules",
        stage="3.1",
        run_stage=RunStage.GUIDELINE,
        depends_on=("3.5.1", "3.1.1"),
        gate=True,
        gate_key=VISUAL_GATE,
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=VisualIdentityOutput,
        connectors=("brand_book",),
    )

    def needs(self) -> tuple[gather.Need, ...]:
        """Three kinds from one connector, so three separate pull keys.

        `brand_book` writes spans, assets and colours from one parse. Without
        distinct `pull_key`s the once-per-run guard would let the first need
        pull and silently skip the other two — the same trap `keyword_metrics`
        documents in `gather.Need`.
        """
        return (
            gather.Need(
                "brand_book_span",
                connector="brand_book",
                optional=True,
                pull_key="brand_book:parse",
            ),
            gather.Need(
                "brand_book_asset",
                connector="brand_book",
                optional=True,
                pull_key="brand_book:parse",
            ),
            gather.Need(
                "brand_book_colour",
                connector="brand_book",
                optional=True,
                pull_key="brand_book:parse",
            ),
        )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(ctx, *self.needs())
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        spans = [row for row in ev if row.kind == "brand_book_span"]
        assets = [row for row in ev if row.kind == "brand_book_asset"]
        observed = _observed_colours(ev)

        draft = await ctx.complete(
            VisualDraft,
            system=prompts.system_prompt(
                "You write the visual rules for a brand: how its logo may be used, "
                "which colours are its own, and what its imagery may show. Ratios and "
                "pixel sizes are measurements — state one only if the source says it, "
                "and otherwise leave it empty. Use only colours you have been shown."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "what the brand book says (empty if it could not be read)",
                    [redact_pii(row.content_text or "") for row in spans],
                ),
                prompts.computed_block("colours sampled from the brand's own assets", observed),
                prompts.computed_block(
                    "the voice profile these visuals sit beside", ctx.outputs.get("3.1.1") or {}
                ),
            ),
        )

        kept, dropped = _sift_colours(draft.colour.tokens, observed)
        confidence = _confidence(spans, observed)
        return VisualIdentityOutput(
            logo=draft.logo.model_copy(
                update={"assets": [str(row.id) for row in assets]},
            ),
            colour=draft.colour.model_copy(update={"tokens": kept}),
            imagery=draft.imagery,
            extraction_confidence=confidence,
            derivation_note=(
                "These rules were read from the brand book."
                if confidence == "high"
                else "The brand book could not be read, so these rules were inferred from "
                "live creative and the brand's own published assets. Confirm them against "
                "your own document before approving."
            ),
            dropped_colours=dropped,
        )


def _observed_colours(rows: list[Evidence]) -> list[str]:
    """Every hex an extractor actually sampled, deduped and ordered."""
    found: list[str] = []
    for row in rows:
        if row.kind != "brand_book_colour":
            continue
        value = str((row.payload or {}).get("hex") or row.content_text or "").strip().lower()
        if value and value not in found:
            found.append(value)
    return found


def _sift_colours(
    tokens: list[ColourToken], observed: list[str]
) -> tuple[list[ColourToken], list[str]]:
    """Keep the colours somebody sampled; report the ones the model chose.

    With nothing observed at all there is nothing to check against, so the
    tokens stand — a degraded brand book must not silently empty the palette,
    and `extraction_confidence` is what tells the approver to look twice.
    """
    if not observed:
        return list(tokens), []
    allowed = set(observed)
    kept = [token for token in tokens if token.hex.strip().lower() in allowed]
    dropped = [token.hex for token in tokens if token.hex.strip().lower() not in allowed]
    return kept, dropped


def _confidence(spans: list[Evidence], observed: list[str]) -> Literal["high", "medium", "low"]:
    if spans and observed:
        return "high"
    if spans or observed:
        return "medium"
    return "low"


visual_identity_rules = VisualIdentityNode()
