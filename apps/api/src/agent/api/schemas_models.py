"""The model picker's data (PRD §13.4 step 3, §14 `GET /models`)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from agent.llm.router import TaskClass


class ModelOption(BaseModel):
    """One row of the searchable model combobox."""

    id: str = Field(description="The OpenRouter model id, e.g. anthropic/claude-haiku-4.5")
    name: str
    context_length: int | None = None
    prompt_per_million: Decimal | None = Field(
        default=None, description="USD per million prompt tokens"
    )
    completion_per_million: Decimal | None = Field(
        default=None, description="USD per million completion tokens"
    )
    supports_structured_output: bool = Field(
        description="PRD §8: a model without it can only ever be a fallback"
    )


class TaskClassRouting(BaseModel):
    """What one task class is for, what it defaults to, and what it consumes."""

    task_class: TaskClass
    label: str
    purpose: str
    default_model: str = Field(description="The seed model, used when nothing is chosen")
    fallbacks: list[str]
    calls_per_run: int
    token_in_per_run: int
    token_out_per_run: int
    usage_source: str = Field(
        description="`measured` from completed runs, or `assumed` until there are enough"
    )


class ModelListResponse(BaseModel):
    """The catalogue plus everything needed to price a routing choice."""

    models: list[ModelOption]
    task_classes: list[TaskClassRouting]
    #: Echoed so the interface can say where the numbers came from without
    #: inspecting every row.
    usage_source: str
