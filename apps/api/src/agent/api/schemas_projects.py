"""Request and response models for the project routes (PRD §14).

A project is the unit a run is launched against, so these shapes carry one
thing beyond the row itself: `requirements`, the server's own answer to "can
this project be run yet". The wizard's final step disables its button from that
list rather than re-deriving the rule in the browser, which is the difference
between one definition of readiness and two that drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from agent.db.models import ApprovalRequiredRole, RunMode, RunStatus, RunTrigger
from agent.gates import gate_ids
from agent.llm.router import TaskClass

Name = Annotated[str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)]
Domain = Annotated[str, StringConstraints(min_length=3, max_length=253, strip_whitespace=True)]

#: Keys inside `Project.settings`. `models` is fixed by `llm.router.SETTINGS_KEY`
#: — the router reads it at run time, so renaming it here would silently strip
#: every override.
SETTINGS_MODELS = "models"


def normalise_domain(value: str) -> str:
    """Accept a pasted URL, store a hostname.

    People paste `https://example.com/en/` out of the address bar. Storing that
    would make two projects for one brand and break every comparison the
    connectors do by domain.
    """
    candidate = value.strip().lower()
    for prefix in ("https://", "http://"):
        candidate = candidate.removeprefix(prefix)
    candidate = candidate.split("/", 1)[0].removeprefix("www.").strip(".")
    if "." not in candidate or " " in candidate:
        raise ValueError(f"{value!r} is not a domain name")
    return candidate


# --- the pieces a project is made of ----------------------------------------


class Market(BaseModel):
    """One country the campaign will run in. PRD §6: `[{country, language, currency}]`."""

    country: Annotated[str, StringConstraints(min_length=2, max_length=2, to_upper=True)] = Field(
        description="ISO 3166-1 alpha-2, e.g. NO"
    )
    language: Annotated[str, StringConstraints(min_length=2, max_length=5)] = Field(
        description="ISO 639-1, e.g. nb"
    )
    currency: Annotated[str, StringConstraints(min_length=3, max_length=3, to_upper=True)] = Field(
        description="ISO 4217, e.g. NOK"
    )


class ProductContext(BaseModel):
    """What we sell, in the words a research node will be grounded on.

    Free text plus the structured fields the nodes actually index on. Everything
    is optional at the model level and required at the *readiness* level — a
    half-filled project is a legitimate thing to save and a bad thing to run.
    """

    summary: Annotated[str, StringConstraints(max_length=8000)] = ""
    products: list[Annotated[str, StringConstraints(max_length=200)]] = Field(default_factory=list)
    pricing: Annotated[str, StringConstraints(max_length=4000)] = ""
    icp: Annotated[str, StringConstraints(max_length=4000)] = Field(
        default="", description="Who we are trying to reach"
    )
    differentiators: list[Annotated[str, StringConstraints(max_length=300)]] = Field(
        default_factory=list
    )
    site_url: Annotated[str, StringConstraints(max_length=2048)] = ""


class GateAssignment(BaseModel):
    """Who decides one gate, and how long they have."""

    assignee_id: uuid.UUID | None = Field(
        default=None, description="Unassigned means any approver may claim it"
    )
    sla_hours: int | None = Field(default=None, ge=1, le=720)


class GateInfo(GateAssignment):
    """A gate as the setup wizard and the project overview render it."""

    node_id: str
    stage: str
    name: str
    audience: str
    description: str
    required_role: ApprovalRequiredRole
    assignee_name: str | None = None


class ModelRouting(BaseModel):
    """Per-task-class model overrides. Absent classes fall back to the seeds."""

    model_config = ConfigDict(extra="forbid")

    extract: str | None = None
    classify: str | None = None
    synthesize: str | None = None
    critique: str | None = None

    @field_validator("*")
    @classmethod
    def _looks_like_a_model_id(cls, value: str | None) -> str | None:
        """Reject a typo here rather than at run time, where it costs money.

        `llm.router` raises on a malformed override when a run resolves it; by
        then the person who typed it has left the screen.
        """
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        if "/" not in candidate or candidate.startswith("/"):
            raise ValueError(f"{value!r} is not an OpenRouter model id (expected 'vendor/model')")
        return candidate

    def as_settings(self) -> dict[str, str]:
        """The `{task_class: model_id}` object `llm.router` reads."""
        return {
            task_class.value: model
            for task_class in TaskClass
            if (model := getattr(self, task_class.value)) is not None
        }

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> ModelRouting:
        """Read back what `as_settings` wrote, ignoring anything it did not.

        Tolerant on the way out on purpose: a stored override for a task class
        this build no longer has should not make the project unreadable.
        """
        raw = (settings or {}).get(SETTINGS_MODELS)
        if not isinstance(raw, dict):
            return cls()
        known = {task_class.value for task_class in TaskClass}
        return cls(
            **{
                key: value
                for key, value in raw.items()
                if key in known and isinstance(value, str) and value
            }
        )


# --- requests ---------------------------------------------------------------


class CreateProjectRequest(BaseModel):
    """The minimum a project needs to exist. Everything else is the wizard's job."""

    name: Name
    domain: Domain

    @field_validator("domain")
    @classmethod
    def _bare_hostname(cls, value: str) -> str:
        return normalise_domain(value)


class UpdateProjectRequest(BaseModel):
    """A partial update. Only the fields present are written."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    domain: Domain | None = None
    product_context: ProductContext | None = None
    markets: list[Market] | None = Field(default=None, max_length=25)
    models: ModelRouting | None = None
    approvals: dict[str, GateAssignment] | None = None

    @field_validator("domain")
    @classmethod
    def _bare_hostname(cls, value: str) -> str:
        return normalise_domain(value)

    @field_validator("approvals")
    @classmethod
    def _known_gates(
        cls, value: dict[str, GateAssignment] | None
    ) -> dict[str, GateAssignment] | None:
        if value is None:
            return None
        known = gate_ids()
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(
                f"{', '.join(unknown)} is not an approval gate; "
                f"the gates are {', '.join(sorted(known))}"
            )
        return value


# --- responses --------------------------------------------------------------


class RunSummary(BaseModel):
    """A run as a list row: what happened, who asked, what it cost."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    status: RunStatus
    mode: RunMode
    trigger: RunTrigger
    triggered_by: uuid.UUID | None = None
    triggered_by_name: str | None = Field(
        default=None, description="Null for a scheduled run, which has no actor"
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cost_usd: Decimal = Decimal("0")
    token_in: int = 0
    token_out: int = 0
    error: dict[str, Any] | None = None


class ProjectRequirement(BaseModel):
    """One thing standing between this project and a run."""

    code: str = Field(description="Stable id, e.g. `openrouter_credential`")
    detail: str = Field(description="The sentence to show the person who has to fix it")
    blocking: bool = Field(
        description="False for a warning that degrades a run without stopping it"
    )


class ProjectSummary(BaseModel):
    """A project as the list renders it."""

    id: uuid.UUID
    name: str
    domain: str
    created_at: datetime
    updated_at: datetime
    created_by: uuid.UUID
    created_by_name: str | None = None
    version: str = Field(
        description="Opaque revision token. Send it back as `If-Match` to save safely."
    )
    run_count: int = 0
    last_run: RunSummary | None = None


class ProjectDetail(ProjectSummary):
    """Everything the overview and the setup wizard need, in one response."""

    product_context: ProductContext
    markets: list[Market]
    models: ModelRouting
    gates: list[GateInfo]
    requirements: list[ProjectRequirement] = Field(
        description="Empty means this project can be run"
    )


class ProjectListResponse(BaseModel):
    projects: list[ProjectSummary]


class RunListResponse(BaseModel):
    runs: list[RunSummary]
