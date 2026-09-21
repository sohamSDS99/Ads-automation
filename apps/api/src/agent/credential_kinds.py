"""What each source is made of, which variables supply it, and how to prove it works.

Every secret this product uses is read from the deployment's environment. There
is no form, no paste, and no per-workspace copy of a key: an operator writes the
variables into `.env` (or into Railway's variables) once, and the interface's
only decision is whether a workspace may use what is already there.

That is the whole shape of this module. A `FieldSpec` names one value *and the
variable that carries it*; a `KindSpec` gathers the fields one connector needs.
`from_env` turns the pair into the `dict[str, str]` that
`ConnectorContext.credentials` expects, which is the same shape
`nodes/gather.py` used to decode out of the vault — connectors did not change
and did not need to.

`meta` is what may be shown about a configured source. A secret is never
readable back, so the interface can only say *which* account is in use from the
non-secret fields plus a last-4 of the first secret. Nothing else ever goes
there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent.db.models import CredentialKind

if TYPE_CHECKING:  # pragma: no cover - import cycle: config imports nothing from here
    from agent.config import Settings


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One value inside a source's credential, and where it is read from."""

    name: str
    label: str
    #: The environment variable carrying this value. `config.Settings` reads it
    #: into a field named by lowercasing this, and `test_source_env.py` asserts
    #: the two agree — a name that drifts would otherwise show up as a source
    #: that is silently unconfigured rather than as an error.
    env_var: str
    required: bool = True
    #: Secret fields are never returned by any endpoint and never reach `meta`.
    secret: bool = True
    hint: str = ""


@dataclass(frozen=True, slots=True)
class KindSpec:
    """A source, as the Connections screen renders it."""

    kind: CredentialKind
    label: str
    description: str
    fields: tuple[FieldSpec, ...]
    #: Which connector's `test_connection()` proves it. None = tested directly.
    connector: str | None
    #: True when a run cannot start without it. Only the model surface is:
    #: every node is a call through OpenRouter, while every other source thins
    #: the report rather than stopping it (PRD §15 NF4, §16).
    required_for_runs: bool = False

    @property
    def env_vars(self) -> tuple[str, ...]:
        """Every variable this source reads, in the order the screen lists them."""
        return tuple(field.env_var for field in self.fields)

    @property
    def required_env_vars(self) -> tuple[str, ...]:
        return tuple(field.env_var for field in self.fields if field.required)

    def from_env(self, settings: Settings) -> dict[str, str]:
        """This source's values as the deployment supplied them.

        Only the fields that actually have a value, so an optional one that was
        left blank is absent rather than empty — `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
        is the case that matters: the Google Ads connector sends the
        `login-customer-id` header if the key is present, and an empty header is
        not the same request as no header.
        """
        found: dict[str, str] = {}
        for field in self.fields:
            value = _setting(settings, field.env_var)
            if value:
                found[field.name] = value
        return found

    def missing_env_vars(self, settings: Settings) -> tuple[str, ...]:
        """The required variables this deployment has not set."""
        return tuple(
            field.env_var
            for field in self.fields
            if field.required and not _setting(settings, field.env_var)
        )

    def configured(self, settings: Settings) -> bool:
        """Whether the environment supplies enough to use this source at all."""
        return not self.missing_env_vars(settings)

    def meta(self, values: dict[str, str]) -> dict[str, Any]:
        """The showable hints: the non-secret fields, plus a last-4 of the first secret."""
        hints: dict[str, Any] = {
            field.name: values[field.name]
            for field in self.fields
            if not field.secret and values.get(field.name)
        }
        first_secret = next((field for field in self.fields if field.secret), None)
        if first_secret is not None and values.get(first_secret.name):
            hints["last4"] = values[first_secret.name][-4:]
        return hints


def _setting(settings: Settings, env_var: str) -> str:
    """One variable's value, whatever type `Settings` holds it as.

    The field name is the variable lowercased rather than a second mapping: a
    third place holding the same nine names is a third place for them to
    disagree, and the disagreement would read as an unconfigured source.
    """
    value = getattr(settings, env_var.lower(), None)
    if value is None:
        return ""
    text = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    return text.strip()


KIND_SPECS: dict[CredentialKind, KindSpec] = {
    CredentialKind.OPENROUTER: KindSpec(
        kind=CredentialKind.OPENROUTER,
        label="OpenRouter",
        description=(
            "One key reaches every model. Every step of the research is a call "
            "through it, so nothing runs until this is connected."
        ),
        fields=(
            FieldSpec(
                name="api_key",
                label="API key",
                env_var="OPENROUTER_API_KEY",
                hint="Starts with sk-or-",
            ),
        ),
        connector=None,
        required_for_runs=True,
    ),
    CredentialKind.GOOGLE_ADS: KindSpec(
        kind=CredentialKind.GOOGLE_ADS,
        label="Google Ads",
        description=(
            "Your own spend, conversions, search terms and past creative, read "
            "from the account the deployment's credentials reach."
        ),
        fields=(
            FieldSpec(
                name="developer_token",
                label="Developer token",
                env_var="GOOGLE_ADS_DEVELOPER_TOKEN",
                hint="Issued once, in the manager account's API Center",
            ),
            FieldSpec(
                name="client_id",
                label="OAuth client ID",
                env_var="GOOGLE_ADS_CLIENT_ID",
            ),
            FieldSpec(
                name="client_secret",
                label="OAuth client secret",
                env_var="GOOGLE_ADS_CLIENT_SECRET",
            ),
            FieldSpec(
                name="refresh_token",
                label="Refresh token",
                env_var="GOOGLE_ADS_REFRESH_TOKEN",
                hint="Minted once by scripts/google-ads-oauth.py",
            ),
            FieldSpec(
                name="customer_id",
                label="Customer ID",
                env_var="GOOGLE_ADS_CUSTOMER_ID",
                secret=False,
                hint="The 10-digit account id, with or without dashes",
            ),
            FieldSpec(
                name="login_customer_id",
                label="Manager (MCC) ID",
                env_var="GOOGLE_ADS_LOGIN_CUSTOMER_ID",
                required=False,
                secret=False,
                hint="Only when the account is reached through a manager account",
            ),
        ),
        connector="google_ads",
    ),
    CredentialKind.DATAFORSEO: KindSpec(
        kind=CredentialKind.DATAFORSEO,
        label="DataForSEO",
        description=(
            "What people actually search for: keyword volume, cost per click, "
            "competition, and twelve months of seasonality."
        ),
        fields=(
            FieldSpec(
                name="api_key",
                label="API key",
                env_var="DATAFORSEO_API_KEY",
                hint="The Basic token on the API Access page. A login:password pair works too.",
            ),
        ),
        connector="dataforseo",
    ),
    CredentialKind.WEBSHARE: KindSpec(
        kind=CredentialKind.WEBSHARE,
        label="Webshare",
        description=(
            "The exit IPs a crawl leaves through, so reading 500 pages does "
            "not arrive at one site as 500 requests from one address."
        ),
        fields=(
            FieldSpec(
                name="api_key",
                label="API key",
                env_var="WEBSHARE_API_KEY",
                hint=(
                    "The key from the Webshare dashboard. The proxy username, "
                    "password and host are all read from it, so this is the whole "
                    "credential."
                ),
            ),
        ),
        # Tested by `connectors/proxy.probe`, not by a connector: the thing
        # proven is a transport several connectors borrow, and no single one of
        # them owns it. `routes_connections._run_test` dispatches on the kind
        # for exactly that reason.
        connector=None,
    ),
}

#: SMTP is deliberately absent, for the reason that now governs everything here:
#: it belongs to the deployment, not to the workspace (PRD §18 law 9). The
#: difference is that SMTP has nothing to switch on or off per workspace, so it
#: needs no card.
CONNECTABLE_KINDS: tuple[CredentialKind, ...] = tuple(KIND_SPECS)


def spec_for(kind: CredentialKind) -> KindSpec:
    try:
        return KIND_SPECS[kind]
    except KeyError:
        raise ValueError(f"{kind.value} is not a connectable source") from None
