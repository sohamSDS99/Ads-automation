"""Stage 1.5 — check we are actually ready (PRD §10).

The four nodes that decide whether the plan the first four stages produced can
survive contact with real traffic. They are the least glamorous nodes in the DAG
and the ones most likely to stop a launch, which is the point: everything before
this describes an opportunity, and this stage asks whether the landing pages
load, whether a conversion would be recorded if someone converted, whether the
audiences are lawful to use, and what the money actually buys.

**Two of the four make no model call**, for the same reason stage 1.4's
`demand_metrics` does not: their entire output is measurement. A page's LCP, a
conversion action's staleness and a synthetic probe's verdict are facts with
thresholds, and routing them through a model would add cost, latency and a way
to be wrong while buying nothing. The arithmetic and the thresholds live in
`nodes/readiness.py`, which is unit-tested without a database or a browser.

The two that *do* call a model ask it only for what measurement cannot answer:

* 1.5.1 asks whether a page keeps the promise the keyword cluster makes. No
  threshold sees a page that is fast, mobile, tagged and about the wrong thing.
* 1.5.3 asks for the lawful basis under which an audience may be used. The Ads
  API reports a list's size and eligibility; it has no field for consent, and a
  connector inventing one is exactly the failure this gate exists to prevent.

1.5.4 is the third pattern — `opportunity_sizing` computes every figure in
Python and asks the model only for the assumptions prose PRD §10 assigns it.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from agent.db.models import ApprovalRequiredRole, Evidence
from agent.llm.router import TaskClass
from agent.nodes import gather, prompts, readiness
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.nodes.gather import Gathered
from agent.nodes.stage_1_1 import CAMPAIGN_PERF, CRM_LOST, CRM_WON, PAGE

log = structlog.get_logger(__name__)

#: Evidence kinds this stage introduces. `page_vitals` is written by the same
#: crawl that writes `page`; the other two are new Google Ads pulls (§9.1).
PAGE_VITALS = "page_vitals"
CONVERSION_ACTION = "conversion_action"
AUDIENCE_LIST = "audience_list"
CONVERSION_PROBE = "conversion_probe"

#: Where a project stores the page a conversion fires on. Nothing writes it yet
#: — the wizard field is P7's — so 1.5.2 degrades to "inconclusive, and here is
#: what to set" rather than guessing at a thank-you URL.
PROBE_URL_SETTING = "conversion_probe_url"

#: Pages audited. The crawl is a real browser measuring Core Web Vitals one page
#: at a time, so this is a time budget as much as a size one; the mapped pages
#: are ordered by the search volume behind them, so the cut is always "the rest
#: carry less demand than these".
MAX_AUDITED_PAGES = 25

#: Days of conversion history the account is asked for. Matches the connector's
#: own `RECENT_DAYS`, and is stated in the output so a reader knows what "no
#: conversions" was measured over.
CONVERSION_WINDOW_DAYS = 90


# ---------------------------------------------------------------------------
# 1.5.1 — landing_page_audit
# ---------------------------------------------------------------------------


class PageMatch(BaseModel):
    """The model's read on one page, and nothing it could have measured."""

    url: str
    message_match: Literal["strong", "partial", "mismatch"] = "partial"
    note: str = Field(
        default="",
        description="One sentence: what the page promises versus what the keywords ask for.",
    )


class PageMatches(BaseModel):
    pages: list[PageMatch] = Field(default_factory=list)


class AuditedPage(BaseModel):
    """1.5.1 output row — PRD §10's fields, every one of them measured."""

    url: str
    lcp_ms: float | None = None
    cls: float | None = None
    mobile_ok: bool | None = None
    form_fields_count: int | None = None
    trust_signals: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    severity: Literal["critical", "major", "minor", "none"] = "none"
    status: int | None = None
    mapped_clusters: list[str] = Field(default_factory=list)
    message_match: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class LandingPageAudit(BaseModel):
    """1.5.1 output."""

    pages: list[AuditedPage] = Field(default_factory=list)
    unreachable: list[str] = Field(
        default_factory=list,
        description="Pages 1.4.5 mapped that could not be fetched at all.",
    )
    blocking: int = 0
    coverage: list[str] = Field(default_factory=list)


class LandingPageAuditNode(LLMNode):
    """1.5.1 — are the pages we would send paid traffic to fit to receive it?

    It audits the pages node 1.4.5 chose, not the site: a crawl of everything
    would spend its whole budget on blog posts no campaign will ever link to.
    """

    spec = NodeSpec(
        id="1.5.1",
        name="landing_page_audit",
        stage="1.5",
        depends_on=("1.4.5",),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=LandingPageAudit,
        connectors=("web_crawler",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        targets = _mapped_urls(ctx)
        needs = []
        if targets:
            # One pull, two kinds: the vitals pass crawls the pages and measures
            # them in the same browser session, and asking for `page` separately
            # would crawl the same URLs twice.
            needs.append(
                gather.Need(
                    PAGE_VITALS,
                    connector="web_crawler",
                    params={
                        "domain": ctx.project.domain,
                        "urls": targets,
                        "vitals": True,
                        "kinds": [PAGE_VITALS, "page"],
                    },
                    refresh=True,
                    pull_key="web_crawler:landing_pages",
                    limit=200,
                )
            )
        needs.append(gather.Need(PAGE, limit=500))
        found = await gather.collect(ctx, *needs)
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        wanted = _mapped_clusters(ctx)
        rows, unreachable = readiness.audit_pages(
            found.payloads(PAGE),
            [row.id for row in found.of(PAGE)],
            vitals=found.payloads(PAGE_VITALS),
            vitals_ids=[row.id for row in found.of(PAGE_VITALS)],
            wanted=wanted,
        )
        # Only the mapped pages are worth a model's attention: an unmapped page
        # has no cluster to be a mismatch with.
        audited = [row for row in rows if row.mapped_clusters][:MAX_AUDITED_PAGES]
        if audited:
            rows = readiness.apply_message_match(rows, await self._matches(ctx, audited, found))

        payloads = [row.as_dict() for row in rows if row.mapped_clusters or not wanted]
        await ctx.progress(f"audited {len(payloads)} pages, {len(unreachable)} unreachable")
        return LandingPageAudit(
            pages=[AuditedPage.model_validate(row) for row in payloads],
            unreachable=unreachable,
            blocking=sum(1 for row in payloads if row["severity"] == "critical") + len(unreachable),
            coverage=gather.coverage_notes(found),
        )

    async def _matches(
        self, ctx: RunContext, rows: list[readiness.PageRow], found: Gathered
    ) -> dict[str, tuple[str, str]]:
        """Ask only about the promise. Everything else on the page is measured."""
        excerpts = {str(payload.get("url") or ""): payload for payload in found.payloads(PAGE)}
        result = await ctx.complete(
            PageMatches,
            system=prompts.system_prompt(
                "You judge whether a landing page keeps the promise a set of search keywords "
                "makes. You never comment on speed, layout or tracking — those are measured "
                "elsewhere and your opinion on them would be noise."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "pages and the keyword clusters routed to them",
                    [
                        {
                            "url": row.url,
                            "clusters": list(row.mapped_clusters),
                            "title": (excerpts.get(row.url) or {}).get("title"),
                            "h1": (excerpts.get(row.url) or {}).get("h1"),
                            "primary_cta": (excerpts.get(row.url) or {}).get("primary_cta"),
                            "excerpt": str((excerpts.get(row.url) or {}).get("text_excerpt") or "")[
                                :700
                            ],
                        }
                        for row in rows
                    ],
                ),
                "TASK\n"
                "  For each page, copy `url` exactly and answer one question: does what this "
                "page says match what someone typing those keywords is looking for?\n"
                "  strong — the page is about exactly that.\n"
                "  partial — related, but the visitor has to work to see it.\n"
                "  mismatch — the page is about something else.\n"
                "  Add one sentence naming the gap. Say nothing about page speed or forms.",
            ),
        )
        return {
            _normalise(item.url): (item.message_match, item.note)
            for item in result.pages
            if item.url
        }


# ---------------------------------------------------------------------------
# 1.5.2 — tracking_probe
# ---------------------------------------------------------------------------


class ConversionActionOut(BaseModel):
    name: str
    conversion_action_id: str | None = None
    status: str | None = None
    category: str | None = None
    primary_for_goal: bool = False
    send_to: str | None = None
    conversions: float = 0.0
    last_conversion_at: str | None = None
    staleness_days: int | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class SyntheticCheckOut(BaseModel):
    """PRD §10's `synthetic_check`, plus the detail that makes it actionable."""

    fired_at: str | None = None
    observed_in_ads_api: bool | None = None
    latency_min: float | None = None
    verdict: Literal["pass", "fail", "inconclusive"] = "inconclusive"
    probe_url: str | None = None
    conversion_action: str | None = None
    tag_ids: list[str] = Field(default_factory=list)
    detail: str = ""


class TrackingProbe(BaseModel):
    """1.5.2 output."""

    conversion_actions: list[ConversionActionOut] = Field(default_factory=list)
    synthetic_check: SyntheticCheckOut = Field(default_factory=SyntheticCheckOut)
    alerts: list[str] = Field(default_factory=list)
    window_days: int = CONVERSION_WINDOW_DAYS
    coverage: list[str] = Field(default_factory=list)


class TrackingProbeNode:
    """1.5.2 — would a conversion be recorded if somebody converted right now?

    No model call anywhere in this node, deliberately. Every field it emits is a
    date subtraction, a status string or the observed behaviour of a real
    browser, and a model asked to restate those could only introduce error.
    """

    spec = NodeSpec(
        id="1.5.2",
        name="tracking_probe",
        stage="1.5",
        depends_on=(),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=TrackingProbe,
        connectors=("google_ads", "web_crawler"),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        needs = [
            gather.Need(
                CONVERSION_ACTION,
                connector="google_ads",
                params={"kinds": [CONVERSION_ACTION]},
                limit=5_000,
            )
        ]
        probe_url = _probe_url(ctx)
        if probe_url:
            needs.append(
                gather.Need(
                    CONVERSION_PROBE,
                    connector="web_crawler",
                    params={"probe_url": probe_url, "domain": ctx.project.domain},
                    # A probe is an experiment, not a lookup: reusing last week's
                    # result would report tracking that has since broken as
                    # working, which is the one thing this node must never do.
                    refresh=True,
                    pull_key="web_crawler:conversion_probe",
                    limit=5,
                )
            )
        found = await gather.collect(ctx, *needs)
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        actions = readiness.fold_conversion_actions(
            found.payloads(CONVERSION_ACTION),
            [row.id for row in found.of(CONVERSION_ACTION)],
        )
        alerts = readiness.tracking_alerts(actions, window_days=CONVERSION_WINDOW_DAYS)
        probes = found.payloads(CONVERSION_PROBE)
        check, probe_alerts = readiness.synthetic_check(probes[0] if probes else None, actions)

        await ctx.progress(f"{len(actions)} conversion actions, synthetic check {check['verdict']}")
        return TrackingProbe(
            conversion_actions=[
                ConversionActionOut.model_validate(row.as_dict()) for row in actions
            ],
            synthetic_check=SyntheticCheckOut.model_validate(check),
            alerts=[*alerts, *probe_alerts],
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.5.3 ⛳ — audience_consent_check
# ---------------------------------------------------------------------------


class ConsentVerdict(BaseModel):
    """One list, judged. Sizes are not asked for — they are measured."""

    name: str
    consent_basis: str = Field(
        default="",
        description="The lawful basis this list may be used under, or why none is established.",
    )
    markets_allowed: list[str] = Field(default_factory=list)
    usable: bool = False
    blocker: str = Field(default="", description="What stops it being used, if anything.")
    evidence_ids: list[str] = Field(default_factory=list)


class ConsentVerdicts(BaseModel):
    lists: list[ConsentVerdict] = Field(default_factory=list)
    reviewer_notes: str = Field(
        default="", description="What the data officer should check before deciding."
    )
    open_questions: list[str] = Field(default_factory=list)


class AudienceList(BaseModel):
    """1.5.3 output row — PRD §10's shape, measured and judged halves merged."""

    name: str
    size: int | None = None
    consent_basis: str | None = None
    markets_allowed: list[str] = Field(default_factory=list)
    usable: bool = False
    blocker: str | None = None
    list_type: str | None = None
    membership_status: str | None = None
    eligible_for_search: bool | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class AudienceConsentCheck(BaseModel):
    """1.5.3 output — the proposal a data officer approves, edits or rejects."""

    lists: list[AudienceList] = Field(default_factory=list)
    markets_in_scope: list[str] = Field(default_factory=list)
    usable_count: int = 0
    reviewer_notes: str = ""
    open_questions: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class AudienceConsentCheckNode(LLMNode):
    """1.5.3 — the gate. Which audiences may lawfully be used, and where.

    The Ads API knows how big a list is and whether it is eligible to serve. It
    does not know why the people on it are there, which is the only question
    that matters for consent — so the measured half comes from the connector,
    the judged half from the model reading our own privacy policy and CRM, and a
    data officer signs the result.
    """

    spec = NodeSpec(
        id="1.5.3",
        name="audience_consent_check",
        stage="1.5",
        depends_on=("1.1.4",),
        gate=True,
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=AudienceConsentCheck,
        connectors=("google_ads",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx,
            gather.Need(
                AUDIENCE_LIST,
                connector="google_ads",
                params={"kinds": [AUDIENCE_LIST]},
                limit=500,
            ),
            # Our own privacy and consent copy is the evidence a basis is cited
            # from, and the CRM export shows what was actually collected.
            gather.Need(PAGE, limit=300),
            gather.Need(CRM_WON, limit=200),
            gather.Need(CRM_LOST, limit=200),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        measured = _audience_inventory(found)
        markets = _markets_in_scope(ctx)
        if not measured:
            return AudienceConsentCheck(
                markets_in_scope=markets,
                reviewer_notes=(
                    "No audience lists were read from the account, so there is nothing to "
                    "approve. Re-run this gate once the Google Ads connector is authorised."
                ),
                coverage=gather.coverage_notes(found),
            )

        verdicts = await ctx.complete(
            ConsentVerdicts,
            system=prompts.system_prompt(
                "You are preparing a data-protection review of advertising audience lists for "
                "a data officer to sign. You state the lawful basis you can evidence and no "
                "more: where our own policy does not establish one, the honest answer is that "
                "it does not, and the list is not usable until someone establishes it."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "audience lists in the account (sizes are final — do not restate them)",
                    measured,
                ),
                prompts.computed_block("markets this project runs in (node 1.1.4)", markets),
                prompts.evidence_block(found, PAGE, title="our own site, incl. policy pages"),
                prompts.evidence_block(found, CRM_WON, title="CRM export (closed won)", limit=10),
                prompts.coverage_block(found),
                prompts.cite_from("the site pages above"),
                "TASK\n"
                "  For each list, copying `name` exactly: the lawful basis its use can be "
                "evidenced under, the markets it may be used in, whether it is usable today, "
                "and the single blocker if it is not. A customer-match list built from a CRM "
                "with no recorded consent is not usable — say so rather than assuming "
                "legitimate interest. Then write one paragraph of reviewer notes naming what "
                "the data officer should check first.",
            ),
        )

        judged = {item.name: item for item in verdicts.lists}
        rows: list[AudienceList] = []
        for item in measured:
            verdict = judged.get(str(item["name"]))
            rows.append(
                AudienceList(
                    name=str(item["name"]),
                    # The size is the connector's, never the model's (law 4).
                    size=item["size"],
                    list_type=item["list_type"],
                    membership_status=item["membership_status"],
                    eligible_for_search=item["eligible_for_search"],
                    consent_basis=(verdict.consent_basis or None) if verdict else None,
                    markets_allowed=list(verdict.markets_allowed) if verdict else [],
                    usable=bool(verdict and verdict.usable),
                    blocker=(verdict.blocker or None)
                    if verdict
                    else "no consent review was returned for this list",
                    evidence_ids=[
                        *item["evidence_ids"],
                        *(verdict.evidence_ids if verdict else []),
                    ],
                )
            )
        return AudienceConsentCheck(
            lists=rows,
            markets_in_scope=markets,
            usable_count=sum(1 for row in rows if row.usable),
            reviewer_notes=verdicts.reviewer_notes,
            open_questions=list(verdicts.open_questions),
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.5.4 — opportunity_sizing
# ---------------------------------------------------------------------------


class ScenarioAssumptions(BaseModel):
    budget_usd_month: float
    assumptions: list[str] = Field(
        default_factory=list,
        description="What has to hold for this scenario to happen. Never a number we gave you.",
    )
    risk: str = Field(default="", description="The single thing most likely to make it wrong.")


class SizingAssumptions(BaseModel):
    scenarios: list[ScenarioAssumptions] = Field(default_factory=list)


class SizedScenario(BaseModel):
    """1.5.4 output row. Every number here came out of Python."""

    budget_usd_month: float
    est_clicks: float | None = None
    est_conv: float | None = None
    est_cpa: float | None = None
    est_revenue: float | None = None
    assumptions: list[str] = Field(default_factory=list)
    confidence_interval: str | None = None
    demand_capped: bool = False
    risk: str | None = None


class OpportunitySizing(BaseModel):
    """1.5.4 output."""

    scenarios: list[SizedScenario] = Field(default_factory=list)
    baseline: dict[str, Any] = Field(default_factory=dict)
    blockers: list[str] = Field(
        default_factory=list, description="Why no scenario could be computed."
    )
    evidence_ids: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class OpportunitySizingNode(LLMNode):
    """1.5.4 — what does the money actually buy?

    PRD §10: "arithmetic in Python, LLM writes assumptions only". The division
    is in `readiness.size()`; the model sees the finished scenarios and writes
    the sentences that say what has to be true for them to happen.
    """

    spec = NodeSpec(
        id="1.5.4",
        name="opportunity_sizing",
        stage="1.5",
        depends_on=("1.4.3", "1.1.1", "1.2.1"),
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=OpportunitySizing,
        connectors=("google_ads",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx,
            gather.Need(
                CAMPAIGN_PERF,
                connector="google_ads",
                params={"kinds": [CAMPAIGN_PERF]},
                limit=5_000,
            ),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        baseline = readiness.account_baseline(
            found.payloads(CAMPAIGN_PERF), economics=ctx.output_of("1.1.1")
        )
        priced = ctx.output_of("1.4.3").get("metrics") or []
        scenarios, blockers = readiness.size(baseline, list(priced))
        evidence_ids = [str(row.id) for row in found.of(CAMPAIGN_PERF)[:12]]

        if not scenarios:
            return OpportunitySizing(
                baseline=baseline.as_dict(),
                blockers=blockers,
                evidence_ids=evidence_ids,
                coverage=gather.coverage_notes(found),
            )

        written = await ctx.complete(
            SizingAssumptions,
            system=prompts.system_prompt(
                "You annotate budget scenarios that have already been computed. You never "
                "state, adjust or round a figure — you write the assumptions each scenario "
                "rests on, in the plainest language you can, so a reader can judge whether "
                "to believe it."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "the measured rates every scenario is built from", baseline.as_dict()
                ),
                prompts.computed_block(
                    "scenarios (final — `demand_capped` means the budget exceeds the demand "
                    "the keyword set carries)",
                    [row.as_dict() for row in scenarios],
                ),
                prompts.computed_block(
                    "what we sell (node 1.1.1)",
                    {
                        key: ctx.output_of("1.1.1").get(key)
                        for key in ("ltv_estimate", "target_cac", "payback_months")
                    },
                ),
                prompts.coverage_block(found),
                "TASK\n"
                "  For each scenario, copying `budget_usd_month` exactly: two to four "
                "assumptions it depends on, and the one risk most likely to break it. Name "
                "the demand cap when it applies, and name any rate that came from somewhere "
                "other than this account's own history. Do not repeat the numbers back.",
            ),
        )
        notes = {round(item.budget_usd_month, 2): item for item in written.scenarios}

        rows: list[SizedScenario] = []
        for scenario in scenarios:
            note = notes.get(round(scenario.budget_usd_month, 2))
            rows.append(
                SizedScenario(
                    **{
                        key: value
                        for key, value in scenario.as_dict().items()
                        if key not in {"inputs"}
                    },
                    assumptions=list(note.assumptions) if note else [],
                    risk=(note.risk or None) if note else None,
                )
            )
        await ctx.progress(f"sized {len(rows)} budget scenarios")
        return OpportunitySizing(
            scenarios=rows,
            baseline=baseline.as_dict(),
            evidence_ids=evidence_ids,
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mapped_clusters(ctx: RunContext) -> dict[str, list[str]]:
    """URL → the keyword clusters node 1.4.5 routed to it, biggest demand first."""
    mapping = ctx.output_of("1.4.5").get("mapping") or []
    ordered = sorted(
        (item for item in mapping if isinstance(item, dict) and item.get("best_url")),
        key=lambda item: -float(item.get("monthly_volume") or 0),
    )
    wanted: dict[str, list[str]] = {}
    for item in ordered:
        url = str(item["best_url"])
        if url not in wanted and len(wanted) >= MAX_AUDITED_PAGES:
            continue
        wanted.setdefault(url, []).append(str(item.get("term_cluster") or ""))
    return wanted


def _mapped_urls(ctx: RunContext) -> list[str]:
    return list(_mapped_clusters(ctx))


def _probe_url(ctx: RunContext) -> str | None:
    value = (ctx.project.settings or {}).get(PROBE_URL_SETTING)
    return str(value).strip() or None if value else None


def _markets_in_scope(ctx: RunContext) -> list[str]:
    markets = ctx.output_of("1.1.4").get("markets") or []
    return [
        str(item.get("country"))
        for item in markets
        if isinstance(item, dict) and item.get("country")
    ]


def _audience_inventory(found: gather.Gathered) -> list[dict[str, Any]]:
    """One record per list, deduped, with the bigger of the two reported sizes."""
    inventory: dict[str, dict[str, Any]] = {}
    for row in found.of(AUDIENCE_LIST):
        payload = dict(row.payload)
        name = str(payload.get("name") or "").strip()
        if not name:
            continue
        size = max(_int(payload.get("size_for_search")), _int(payload.get("size_for_display")))
        record = inventory.setdefault(
            name,
            {
                "name": name,
                "size": size,
                "list_type": payload.get("list_type"),
                "membership_status": payload.get("membership_status"),
                "membership_life_span_days": payload.get("membership_life_span_days"),
                "eligible_for_search": payload.get("eligible_for_search"),
                "description": payload.get("description"),
                "evidence_ids": [],
            },
        )
        record["evidence_ids"].append(str(row.id))
    return sorted(inventory.values(), key=lambda item: -int(item["size"] or 0))


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _normalise(url: str) -> str:
    return url.strip().rstrip("/").lower()


landing_page_audit = LandingPageAuditNode()
tracking_probe = TrackingProbeNode()
audience_consent_check = AudienceConsentCheckNode()
opportunity_sizing = OpportunitySizingNode()
