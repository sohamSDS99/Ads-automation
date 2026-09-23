"""Typed application configuration.

Every environment difference between local Compose and Railway is a VALUE in
here. There is no `if RAILWAY` branch anywhere in application code (PRD §15
NF8c) — the code reads the same names in both places.
"""

from __future__ import annotations

import base64
import binascii
import sys
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENCRYPTION_KEY_BYTES = 32


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


class Settings(BaseSettings):
    """Application settings, read from the environment exactly once."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- core ---------------------------------------------------------------
    # Railway injects PORT. Never hardcode it (PRD §5.2 rule 3).
    port: int = 8000
    app_env: Literal["local", "production"] = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql://agent:agent@postgres:5432/agent"
    redis_url: str = "redis://redis:6379/0"

    # 32 bytes, base64-encoded. Never logged, never returned by any endpoint.
    app_encryption_key: SecretStr = SecretStr("")

    # --- sessions (consumed in P0b) ----------------------------------------
    session_cookie_name: str = "ara_session"
    session_ttl_days: int = 30
    cookie_secure: bool = False

    # --- public URLs --------------------------------------------------------
    # Where a browser reaches this installation. Invite links are built from it,
    # so an unset value in production produces links that resolve to localhost.
    app_base_url: str = "http://localhost:3000"

    # --- bootstrap admin (consumed in P0b) ---------------------------------
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # --- llm ----------------------------------------------------------------
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # --- embeddings ---------------------------------------------------------
    # OpenRouter serves no embedding model (its catalogue is chat-only), so the
    # vectors are produced locally. `EMBEDDING_DIM` in `db.models` is the schema
    # and must equal the provider's width — `tests/test_embedding.py` asserts it.
    #
    # `hash` is a deterministic, dependency-free provider. It exists so unit
    # tests and an offline CI never reach for the ONNX model, and it is not a
    # sensible production choice: it captures token overlap, not meaning.
    embedding_provider: Literal["fastembed", "hash"] = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_batch_size: int = 64

    # --- connectors ---------------------------------------------------------
    # One identity for every outbound connector request. A crawler that does not
    # say who it is gets blocked, and deserves to be.
    connector_user_agent: str = (
        "AdsResearchAgent/0.1 (+https://github.com/sohamSDS99/Ads-automation)"
    )
    connector_timeout_s: float = 30.0
    connector_max_retries: int = 3

    # google_ads: the REST surface, not the gRPC SDK. See `connectors/google_ads.py`.
    # v18 was retired: `POST /v18/customers/…/googleAds:searchStream` answers 404
    # (an HTML one, from the front end — not a JSON API error). Measured
    # 2026-09-17: v17-v21 are gone, v22+ answer 401. v25 is the newest
    # *released* version (sunset August 2027, the longest runway on offer).
    google_ads_api_version: str = "v25"
    google_ads_base_url: str = "https://googleads.googleapis.com"
    google_ads_lookback_months: int = 24

    dataforseo_base_url: str = "https://api.dataforseo.com/v3"

    # webshare: the exit IP an outbound crawl leaves through — `connectors/proxy.py`
    # explains which connectors use it and, more importantly, which measured
    # reasons keep `transparency` and every browser path off it. Shape, not
    # secret: the account itself is `CredentialKind.WEBSHARE` — one API key,
    # from the vault or from `WEBSHARE_API_KEY` below.
    webshare_api_url: str = "https://proxy.webshare.io/api/v2"
    #: The rotating endpoint — one address, a new exit per request. The
    #: thousand-row proxy list is the alternative and it makes us the balancer.
    webshare_proxy_host: str = "p.webshare.io"
    webshare_proxy_port: int = 80
    #: Which country crawl traffic leaves from. Empty means anywhere in the
    #: pool; a country the plan has not allocated is ignored with a log line
    #: rather than sent, because Webshare answers that as an auth failure.
    webshare_country: str = "us"
    #: How long a resolved proxy pair is reused. A 500-URL crawl must resolve
    #: the account once, and the credential cannot change inside a run.
    webshare_cache_ttl_s: float = 900.0
    # web_crawler: PRD §9.4 caps — 500 URLs, depth 3.
    crawl_max_urls: int = 500
    crawl_max_depth: int = 3
    crawl_concurrency: int = 4
    crawl_delay_s: float = 0.2

    # transparency: PRD §9.2 — one page at a time, randomised 2–5s between actions.
    transparency_base_url: str = "https://adstransparency.google.com"
    transparency_min_delay_s: float = 2.0
    transparency_max_delay_s: float = 5.0
    transparency_max_ads: int = 300

    # csv_ingest: a guard against a 2GB upload, not a product limit.
    csv_max_bytes: int = 32 * 1024 * 1024

    # documents: the business-context library uploaded in step 1 of the wizard.
    # Every one of these is a guard rather than a product opinion — the real
    # constraint is prompt budget, and that is enforced per node when the
    # passages are rendered, not here.
    document_max_bytes: int = 20 * 1024 * 1024
    #: Characters kept from one file. Roughly 100k words: longer than any brand
    #: document that is actually about this brand, and short enough that one
    #: upload cannot fill the evidence table.
    document_max_chars: int = 400_000
    #: Files per project. A library larger than this is a content strategy, not
    #: a business context, and the nodes can only read a fraction of it anyway.
    document_max_per_project: int = 25
    # An archive is one upload that becomes many documents, so it needs its own
    # ceilings. The ratio is the zip-bomb guard: a folder of PDFs and Word files
    # is already compressed and expands maybe three or four times, so 120 is far
    # past anything honest and far below what a bomb needs to hurt.
    archive_max_entries: int = 50
    archive_max_total_bytes: int = 200 * 1024 * 1024
    archive_max_ratio: int = 120
    csv_max_rows: int = 200_000

    # --- storage ------------------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: str = "/data"
    worker_internal_url: str = "http://worker:8081"
    #: The port `fileserver.py` binds inside the worker. It must agree with the
    #: port in WORKER_INTERNAL_URL — that URL is how `api` reaches this port.
    file_server_port: int = 8081
    #: Signs the one-object download capabilities of `export/tokens.py`. Unset is
    #: safe: the secret is then derived from APP_ENCRYPTION_KEY rather than
    #: authentication being skipped. Set it to rotate download tokens on their
    #: own schedule.
    file_token_secret: SecretStr = SecretStr("")

    # --- backups and retention (P8) -----------------------------------------
    # Every window is in days and every one can be set to 0 to keep forever.
    # Defaults are deliberately generous for the things that are evidence
    # (screenshots a report cites) and short for the things that are debris
    # (a dumped page, an export anyone can regenerate).
    backup_enabled: bool = True
    backup_retention_days: int = 14
    #: Creative screenshots. Cited by reports, so the window outlives the
    #: quarter a report is read in.
    screenshot_retention_days: int = 90
    #: Connector failure dumps. Useful for a week, noise after that.
    debug_retention_days: int = 14
    #: Rendered exports. Regenerable from the stored report at any time.
    export_retention_days: int = 30
    #: How full the Volume may get before `/settings` shows a banner.
    storage_warn_fraction: float = 0.8

    # --- budget -------------------------------------------------------------
    max_run_cost_usd: Decimal = Decimal("15.00")

    # --- stage 02: the handshake (Stage 02 PRD §4.2, §4.3) ------------------
    #: Which `Report.schema_version` values Stage 02 knows how to read. Version
    #: skew is caught at trigger time and not mid-run (§4.3 rule 5), so this is
    #: read by `/plan/eligibility` and by `build_plan_input`, and by nothing
    #: that runs after a token has been spent.
    plan_supported_research_schemas: frozenset[str] = frozenset({"1.0"})
    #: Past this, acceptance age is a *warning* on the Start button: the plan
    #: is still startable and the forecast behind it is getting old.
    plan_source_max_age_days: int = 30
    #: Past this it is a blocker, and only an `admin` override gets past it.
    #: A 90-day-old demand curve is not evidence, it is a memory.
    plan_source_hard_age_days: int = 90
    #: The per-plan-run ceiling, separate from `max_run_cost_usd`: a plan run
    #: is a different shape of work from a research run and PRD §17 PF4 prices
    #: it separately.
    max_plan_cost_usd: Decimal = Decimal("8.00")

    # --- Stage 03, the content guidelines agent ----------------------------
    #: Which upstream schema versions a *binding* may carry. Deliberately
    #: unlike `plan_supported_research_schemas`, which produces a 422: a
    #: skewed version here drops the binding and the run continues, because
    #: Stage 03 does not need it (PRD §4.5 rule 4). Turning either of these
    #: into a blocker rebuilds Stage 02's handshake by accident.
    guideline_supported_research_schemas: frozenset[str] = frozenset({"1.0"})
    guideline_supported_plan_schemas: frozenset[str] = frozenset({"1.0"})
    #: Lower than planning: no crawling at keyword scale and no forecast pass.
    max_guideline_cost_usd: Decimal = Decimal("6.00")
    #: How long a step-up re-auth proof stays usable. Single-use as well as
    #: short-lived — both halves are asserted in S3-P3.
    signature_reauth_ttl_seconds: int = 300
    #: Stamped into every `RuleSet` and folded into `Run.input_hash`, so a
    #: thresholds bump re-runs the deterministic nodes instead of reusing an
    #: output shaped by the old numbers. S3-P1 replaces this default by reading
    #: `version:` out of `content_constants.yaml`; it lives here now so the
    #: hash is honest before that file exists.
    content_constants_version: str = "2026.09.2"
    #: Ceiling on an image submitted to `POST /guidelines/{id}/lint/image`.
    #: 5,242,880 bytes is Google's own limit for an image asset — "Maximum file
    #: size: 5120 KB" — so anything larger could not be uploaded to Google
    #: whatever the precheck said about it. Checked 2026-09-23 against
    #: support.google.com/google-ads/answer/9566341.
    image_lint_max_bytes: int = 5_242_880

    # --- source keys supplied by the deployment ----------------------------
    # Every secret the product uses, and the only place any of them lives. The
    # interface never accepts a key and has nowhere to put one: what a
    # workspace owns is the decision to use a source (`SourceConnection`), not
    # a copy of its credential.
    #
    # The names are not free. `credential_kinds.FieldSpec.env_var` lowercased is
    # the field read here, and `tests/test_source_env.py` fails if a spec names
    # a variable this class does not hold — a drifting name would otherwise
    # read as a source nobody configured rather than as a mistake.
    openrouter_api_key: SecretStr | None = None
    dataforseo_api_key: SecretStr | None = None
    webshare_api_key: SecretStr | None = None

    # Google Ads is six values, not one. The developer token is issued once in
    # the manager account's API Center; the OAuth three are minted by
    # `scripts/google-ads-oauth.py`, which also prints every customer id the
    # consent reaches. The manager id is needed only when the account is
    # reached through an MCC, and is the one field here that may stay unset.
    google_ads_developer_token: SecretStr | None = None
    google_ads_client_id: SecretStr | None = None
    google_ads_client_secret: SecretStr | None = None
    google_ads_refresh_token: SecretStr | None = None
    google_ads_customer_id: str | None = None
    google_ads_login_customer_id: str | None = None

    # --- smtp (all optional; without it invites fall back to copyable links)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    # --- web ----------------------------------------------------------------
    # Only used by the local Compose mirror. In production the browser is
    # same-origin with `web`, so no CORS entry is needed at all.
    #
    # `NoDecode` matters: without it pydantic-settings tries to JSON-decode any
    # list-typed field coming from the environment, and a plain
    # `CORS_ORIGINS=http://localhost:3000` fails before the validator below ever
    # runs.
    cors_origins: Annotated[
        list[str],
        NoDecode,
        Field(default_factory=lambda: ["http://localhost:3000"]),
    ]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("app_encryption_key")
    @classmethod
    def _validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw:
            raise ValueError(
                "APP_ENCRYPTION_KEY is not set. Generate one with:\n"
                '  python -c "import base64,os;'
                'print(base64.b64encode(os.urandom(32)).decode())"'
            )
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"APP_ENCRYPTION_KEY is not valid base64 ({exc}). It must be "
                f"{ENCRYPTION_KEY_BYTES} random bytes, base64-encoded."
            ) from exc
        if len(decoded) != ENCRYPTION_KEY_BYTES:
            raise ValueError(
                f"APP_ENCRYPTION_KEY must decode to exactly {ENCRYPTION_KEY_BYTES} bytes, "
                f"got {len(decoded)}. Generate one with:\n"
                '  python -c "import base64,os;'
                'print(base64.b64encode(os.urandom(32)).decode())"'
            )
        return value

    @property
    def encryption_key(self) -> bytes:
        """The decoded AES-256 key. Never log this."""
        return base64.b64decode(self.app_encryption_key.get_secret_value(), validate=True)

    @property
    def async_database_url(self) -> str:
        """`DATABASE_URL` normalised to the asyncpg driver.

        Railway hands out `postgresql://…`; SQLAlchemy needs the driver named
        explicitly. Normalising here means one env var works in both places.
        """
        url = self.database_url
        if url.startswith("postgresql+"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        return url

    @field_validator("app_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once, failing fast and legibly if the environment is wrong."""
    try:
        return Settings()
    except ValidationError as exc:
        lines = ["Invalid configuration — the process cannot start:"]
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "(root)"
            lines.append(f"  - {field.upper()}: {error['msg']}")
        message = "\n".join(lines)
        print(message, file=sys.stderr)  # noqa: T201 — startup diagnostics
        raise ConfigError(message) from exc
