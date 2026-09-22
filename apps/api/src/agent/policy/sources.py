"""`policy_sources.yaml`, loaded once and validated hard (PRD §9.7, law 25).

The sibling of `guidelines/constants.py`, and it follows the same rule for the
same reason: **a source without attribution fails startup, and the failure
names the label.** A watched URL is an authority — findings cite it, and
`policy_ref` on a compiled rule points at it — so an unattributable URL is an
unauditable rule.

One rule here is not in the constants loader. `selector` is required and may
not be empty. `PolicySource.selector` narrows the hashed region, and the model
comment says it must never quietly report "no change"; a source with no
selector would hash the whole document including the CSP nonces that change on
every request, which is the failure mode §9.7 exists to avoid.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: Shipped next to the constants file it mirrors, under `guidelines/`, so the
#: two files a reviewer has to read together live together. `Dockerfile` copies
#: `src/`, so there is nothing further to install.
SOURCES_PATH = Path(__file__).parent.parent / "guidelines" / "policy_sources.yaml"


class PolicySourcesError(RuntimeError):
    """The registry file cannot produce a usable set of watched sources."""


class _Node(BaseModel):
    """Closed and frozen. A typo like `selecter:` must fail, not be ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PolicySourceSpec(_Node):
    """One watched page, with the provenance that makes it auditable."""

    label: str = Field(min_length=1)
    url: str = Field(min_length=1)
    area: str = Field(min_length=1)
    #: Required, unlike the nullable DB column. See the module docstring: the
    #: column is nullable because a hand-added source in Settings may not have
    #: one yet, but a *seeded* source without one is a bug we can catch here.
    selector: str = Field(min_length=1)
    source: str = Field(min_length=1)
    reviewed_at: date
    jurisdiction: str | None = None
    poll_cron: str | None = None

    @property
    def verified(self) -> bool:
        """False while the URL is still `source: unverified` (law 25)."""
        return self.source != "unverified"


class PolicySources(_Node):
    version: str = Field(min_length=1)
    poll_default_cron: str = Field(min_length=1)
    sources: tuple[PolicySourceSpec, ...] = Field(min_length=1)

    def cron_for(self, spec: PolicySourceSpec) -> str:
        """The source's own cadence, or the file default."""
        return spec.poll_cron or self.poll_default_cron

    def by_area(self, area: str) -> tuple[PolicySourceSpec, ...]:
        """Every source for one area.

        Plural on purpose: `disclosure` has two, because no single Google page
        carries both the election-ad synthetic content rule and the EU/India/
        New York AI-labelling market list. A caller assuming one would silently
        drop whichever it did not pick.
        """
        return tuple(spec for spec in self.sources if spec.area == area)

    @property
    def unverified(self) -> tuple[PolicySourceSpec, ...]:
        """The sources no human has confirmed yet. Surfaced in Settings."""
        return tuple(spec for spec in self.sources if not spec.verified)


def _describe(error: ValidationError) -> str:
    """Render a validation failure as `sources.3.selector: message`, one per line."""
    lines = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "<root>"
        lines.append(f"  {location}: {detail['msg']}")
    return "\n".join(lines)


def load_policy_sources(path: Path | None = None) -> PolicySources:
    """Read and validate the registry. Raises `PolicySourcesError` on anything."""
    source = path or SOURCES_PATH
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicySourcesError(f"{source} is missing") from exc
    except yaml.YAMLError as exc:
        raise PolicySourcesError(f"{source} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise PolicySourcesError(f"{source} must be a mapping, got {type(raw).__name__}")

    try:
        parsed = PolicySources.model_validate(raw)
    except ValidationError as exc:
        raise PolicySourcesError(f"{source.name} is invalid:\n{_describe(exc)}") from exc

    seen: dict[str, str] = {}
    for spec in parsed.sources:
        if spec.url in seen:
            # `UNIQUE(workspace_id, url)` would reject the second row at seed
            # time with a constraint error naming neither label. Failing here
            # names both, and does it at startup rather than at first seed.
            raise PolicySourcesError(
                f"{source.name} lists {spec.url} twice: {seen[spec.url]!r} and {spec.label!r}. "
                "policy_source is UNIQUE on (workspace_id, url), so the second would be "
                "rejected at seed time. Narrow one with a different page rather than a "
                "different selector onto the same URL."
            )
        seen[spec.url] = spec.label
    return parsed


@lru_cache(maxsize=1)
def get_policy_sources() -> PolicySources:
    """The process-wide registry. Cached like `get_content_constants()`."""
    return load_policy_sources()
