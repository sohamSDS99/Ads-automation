"""What each credential kind is made of, and how to prove it works.

The vault stores one sealed string per row. Most of these secrets are not one
string — `google_ads` needs five values — so a multi-field kind is sealed as a
JSON object and a single-field kind as the bare value. That split is not a
preference: `orchestrator/executor.py` resolves the OpenRouter secret and hands
it straight to `build_gateway(api_key=...)`, and `nodes/gather.py` JSON-decodes
a connector secret into `ConnectorContext.credentials`. Both already exist, so
this module describes them rather than deciding for them.

`meta` is the other half of the design. A credential is never readable back, so
the only way the interface can say *which* account is connected is a hint saved
alongside the ciphertext. Every field marked `secret=False` goes there; nothing
else ever does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent.db.models import CredentialKind


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One value inside a credential."""

    name: str
    label: str
    required: bool = True
    #: Secret fields are write-only everywhere: `type=password` in the browser,
    #: absent from `meta`, absent from every response.
    secret: bool = True
    hint: str = ""


@dataclass(frozen=True, slots=True)
class KindSpec:
    """A credential kind, as the settings and wizard screens render it."""

    kind: CredentialKind
    label: str
    description: str
    fields: tuple[FieldSpec, ...]
    #: Which connector's `test_connection()` proves it. None = tested directly.
    connector: str | None
    #: Where in the product this credential is configured, so the two screens
    #: that write credentials can each show only their own.
    where: str

    @property
    def multi_field(self) -> bool:
        """More than one value, so the sealed secret is a JSON object."""
        return len(self.fields) > 1

    def seal(self, values: dict[str, str]) -> str:
        """The string to encrypt.

        Single-field kinds seal the bare value because the run path expects it:
        `executor._build_gateway` passes the resolved secret to OpenRouter as
        the API key with no decoding step in between.
        """
        if not self.multi_field:
            return values[self.fields[0].name]
        # Sorted so the same credential typed twice seals to the same plaintext,
        # which makes "did this actually change" answerable without decrypting.
        return json.dumps({key: values[key] for key in sorted(values)}, separators=(",", ":"))

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

    def validate(self, values: dict[str, str]) -> dict[str, str]:
        """Keep the known fields, reject the missing required ones.

        Unknown keys are dropped rather than rejected: a client that sends an
        extra field gets a working credential, and a field this build has never
        heard of must not end up sealed into a secret nothing can read.
        """
        known = {field.name for field in self.fields}
        cleaned = {
            name: value.strip()
            for name, value in values.items()
            if name in known and isinstance(value, str) and value.strip()
        }
        missing = [
            field.name for field in self.fields if field.required and field.name not in cleaned
        ]
        if missing:
            raise ValueError(f"missing required value(s): {', '.join(missing)}")
        return cleaned


KIND_SPECS: dict[CredentialKind, KindSpec] = {
    CredentialKind.OPENROUTER: KindSpec(
        kind=CredentialKind.OPENROUTER,
        label="OpenRouter",
        description="One key for every model. Every research node is a call through it.",
        fields=(
            FieldSpec(
                name="api_key",
                label="API key",
                hint="Starts with sk-or-",
            ),
        ),
        connector=None,
        where="settings",
    ),
    CredentialKind.GOOGLE_ADS: KindSpec(
        kind=CredentialKind.GOOGLE_ADS,
        label="Google Ads",
        description="Our own account history: spend, conversions, search terms and past creative.",
        fields=(
            FieldSpec(name="developer_token", label="Developer token"),
            FieldSpec(name="client_id", label="OAuth client ID"),
            FieldSpec(name="client_secret", label="OAuth client secret"),
            FieldSpec(name="refresh_token", label="Refresh token"),
            FieldSpec(
                name="customer_id",
                label="Customer ID",
                secret=False,
                hint="The 10-digit account id, with or without dashes",
            ),
            FieldSpec(
                name="login_customer_id",
                label="Manager (MCC) ID",
                required=False,
                secret=False,
                hint="Only if the account is reached through a manager account",
            ),
        ),
        connector="google_ads",
        where="sources",
    ),
    CredentialKind.DATAFORSEO: KindSpec(
        kind=CredentialKind.DATAFORSEO,
        label="DataForSEO",
        description="Keyword volume, CPC, competition and 12 months of seasonality.",
        fields=(
            FieldSpec(name="login", label="Login", secret=False, hint="The account email"),
            FieldSpec(name="password", label="Password"),
        ),
        connector="dataforseo",
        where="sources",
    ),
}

#: SMTP is deliberately absent. It is read from the environment
#: (`config.Settings.smtp_*`) because it belongs to the deployment, not to the
#: workspace — PRD §18 law 9: environments differ by env-var values only.
WRITABLE_KINDS: tuple[CredentialKind, ...] = tuple(KIND_SPECS)


def spec_for(kind: CredentialKind) -> KindSpec:
    try:
        return KIND_SPECS[kind]
    except KeyError:
        raise ValueError(f"{kind.value} is not configured through the interface") from None


def unseal(spec: KindSpec, secret: str) -> dict[str, str]:
    """The sealed string back as connector-shaped values.

    Mirrors `nodes/gather._credentials`: a JSON object becomes the value map, a
    bare string becomes the kind's single field.
    """
    if not spec.multi_field:
        return {spec.fields[0].name: secret}
    try:
        parsed = json.loads(secret)
    except ValueError:
        return {spec.fields[0].name: secret}
    if not isinstance(parsed, dict):
        return {spec.fields[0].name: secret}
    return {str(key): str(value) for key, value in parsed.items() if value is not None}
