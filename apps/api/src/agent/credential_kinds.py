"""What each source is made of, which variables supply it, and how to prove it works.

Most secrets this product uses are read from the deployment's environment. There
is no form and no paste: an operator writes the variables into `.env` (or into
Railway's variables) once, and the interface's only decision is whether a
workspace may use what is already there.

One source does not fit that shape, and pretending it did is what kept Google
Ads unconnected for months. A Google Ads call needs five values, and only three
of them belong to the deployment — the developer token and the OAuth client.
The other two are a *person's*: the refresh token their consent mints, and the
customer id of the account that consent reaches. Nobody can write those into an
environment on someone else's behalf, and the developer token is issued once to
one manager account, so requiring every person to hold one is requiring most of
them not to connect at all.

So a `FieldSpec` says where its value comes from. `granted=True` means a consent
supplies it, and everything that reads the environment — `missing_env_vars`,
`configured`, the "not set up" card — skips those fields. `KindSpec.values`
merges the two halves back into the single `dict[str, str]` that
`ConnectorContext.credentials` has always expected, so connectors did not change
and did not need to.

`meta` is what may be shown about a configured source. A secret is never
readable back, so the interface can only say *which* account is in use from the
non-secret fields plus a last-4 of the first secret. Nothing else ever goes
there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

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
    #:
    #: A granted field keeps one anyway: a deployment that already minted a
    #: refresh token with `make google-ads-oauth` should keep working, and that
    #: path writes exactly these variables.
    env_var: str
    required: bool = True
    #: Secret fields are never returned by any endpoint and never reach `meta`.
    secret: bool = True
    hint: str = ""
    #: True when a person's consent supplies this, not the operator. The
    #: environment may still carry one as a fallback, but its absence is not a
    #: misconfiguration — it is a source nobody has signed in to yet.
    granted: bool = False


@dataclass(frozen=True, slots=True)
class OAuthSpec:
    """How a person hands this source the half the deployment cannot hold."""

    #: Which consent flow finishes this credential. One provider today; the
    #: field exists so the routes dispatch on data rather than on the kind.
    provider: Literal["google"]
    #: The button, in the words a person reading the card would use.
    action: str
    #: What the consent is for, shown under the button.
    explains: str


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
    #: Set when part of this credential comes from a person's consent.
    oauth: OAuthSpec | None = None

    @property
    def env_vars(self) -> tuple[str, ...]:
        """Every variable this source reads, in the order the screen lists them."""
        return tuple(field.env_var for field in self.fields)

    @property
    def deployment_env_vars(self) -> tuple[str, ...]:
        """The variables an operator is actually expected to set.

        A granted field's variable is a fallback, not an instruction: telling
        someone to put `GOOGLE_ADS_REFRESH_TOKEN` in the environment is telling
        them to go and run a script, which is the whole thing consent replaces.
        """
        return tuple(field.env_var for field in self.fields if not field.granted)

    @property
    def required_env_vars(self) -> tuple[str, ...]:
        return tuple(field.env_var for field in self.fields if field.required and not field.granted)

    @property
    def granted_fields(self) -> tuple[str, ...]:
        """The field names a consent supplies, in the order they are sealed."""
        return tuple(field.name for field in self.fields if field.granted)

    def from_env(self, settings: Settings) -> dict[str, str]:
        """This source's values as the deployment supplied them.

        Only the fields that actually have a value, so an optional one that was
        left blank is absent rather than empty — `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
        is the case that matters: the Google Ads connector sends the
        `login-customer-id` header if the key is present, and an empty header is
        not the same request as no header.

        Granted fields are read here too. The environment is not where they are
        expected to come from, but a deployment that set them before consent
        existed must keep working.
        """
        found: dict[str, str] = {}
        for field in self.fields:
            value = _setting(settings, field.env_var)
            if value:
                found[field.name] = value
        return found

    def values(self, settings: Settings, grant: dict[str, str] | None = None) -> dict[str, str]:
        """Everything a connector needs: the deployment's half, then the person's.

        The grant wins where they overlap. A workspace that signed in to Google
        is using the account it chose, whatever a leftover variable on the
        deployment says — otherwise the first workspace to connect would decide
        for every workspace after it.
        """
        merged = self.from_env(settings)
        for name, value in (grant or {}).items():
            if value:
                merged[name] = value
        return merged

    def missing_env_vars(self, settings: Settings) -> tuple[str, ...]:
        """The required deployment variables this deployment has not set."""
        return tuple(
            field.env_var
            for field in self.fields
            if field.required and not field.granted and not _setting(settings, field.env_var)
        )

    def missing_values(self, values: dict[str, str]) -> tuple[str, ...]:
        """The required *fields* still absent once env and grant are merged.

        Field names, not variable names: what is missing here is not something
        anybody types into an environment.
        """
        return tuple(
            field.name for field in self.fields if field.required and not values.get(field.name)
        )

    def configured(self, settings: Settings) -> bool:
        """Whether the environment supplies enough for this source to be usable at all.

        For an OAuth source this is the deployment's half only — the half a
        person cannot supply. Whether anyone has actually signed in is a
        separate question, answered by the connection row, because the two have
        different fixes and a single boolean would name neither.
        """
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
            "from the Google Ads account you sign in to."
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
                granted=True,
                hint="Minted by signing in to Google. Never typed.",
            ),
            FieldSpec(
                name="customer_id",
                label="Customer ID",
                env_var="GOOGLE_ADS_CUSTOMER_ID",
                secret=False,
                granted=True,
                hint="The account chosen after signing in",
            ),
            FieldSpec(
                name="login_customer_id",
                label="Manager (MCC) ID",
                env_var="GOOGLE_ADS_LOGIN_CUSTOMER_ID",
                required=False,
                secret=False,
                granted=True,
                hint="Set for you when the account is reached through a manager account",
            ),
        ),
        connector="google_ads",
        oauth=OAuthSpec(
            provider="google",
            action="Connect with Google",
            explains=(
                "Sign in with the Google account that can see your Google Ads, "
                "then choose which account to read."
            ),
        ),
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

#: The sources a consent finishes, by provider. `routes_connections` walks this
#: rather than naming Google Ads, so a second Google source — Analytics, Search
#: Console — joins the same flow by gaining an `OAuthSpec`.
OAUTH_KINDS: dict[str, tuple[CredentialKind, ...]] = {
    provider: tuple(
        kind for kind, spec in KIND_SPECS.items() if spec.oauth and spec.oauth.provider == provider
    )
    for provider in {spec.oauth.provider for spec in KIND_SPECS.values() if spec.oauth}
}


def spec_for(kind: CredentialKind) -> KindSpec:
    try:
        return KIND_SPECS[kind]
    except KeyError:
        raise ValueError(f"{kind.value} is not a connectable source") from None
