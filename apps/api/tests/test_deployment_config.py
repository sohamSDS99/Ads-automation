"""The deploy surface: three valid railway.json files, and no api/worker ingress."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "apps" / "api"
WEB_DIR = REPO_ROOT / "apps" / "web"

#: config path -> the service root it is resolved against. `railway.worker.json`
#: sits at the repo root because the worker's build context is the repo root.
RAILWAY_FILES = {
    API_DIR / "railway.json": API_DIR,
    REPO_ROOT / "railway.worker.json": REPO_ROOT,
    WEB_DIR / "railway.json": WEB_DIR,
}

#: The worker's Dockerfile, at the repo root. It cannot live beside the api's in
#: `apps/api`: Railway picks the build file by composing Root Directory +
#: `Dockerfile` and ignores every override, so two services sharing one root
#: directory cannot build two different Dockerfiles.
WORKER_DOCKERFILE = REPO_ROOT / "Dockerfile"


@pytest.mark.parametrize(("config_path", "service_root"), list(RAILWAY_FILES.items()))
def test_railway_config_parses_and_names_a_real_dockerfile(
    config_path: Path, service_root: Path
) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    dockerfile = config["build"]["dockerfilePath"]
    assert (service_root / dockerfile).is_file(), f"{config_path.name} -> missing {dockerfile}"
    assert config["build"]["builder"] == "DOCKERFILE"


def test_api_runs_migrations_before_the_deploy_and_has_a_healthcheck() -> None:
    config = json.loads((API_DIR / "railway.json").read_text(encoding="utf-8"))
    assert config["deploy"]["preDeployCommand"] == "alembic upgrade head"
    assert config["deploy"]["healthcheckPath"] == "/api/v1/health"


def test_worker_has_no_healthcheck() -> None:
    config = json.loads((REPO_ROOT / "railway.worker.json").read_text(encoding="utf-8"))
    assert "healthcheckPath" not in config["deploy"]
    assert config["deploy"]["restartPolicyMaxRetries"] == 10


def test_only_web_publishes_a_host_port() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    published = {
        name: service.get("ports")
        for name, service in compose["services"].items()
        if service.get("ports")
    }
    assert set(published) == {"web"}, f"only `web` may publish a port, got {published}"


def test_api_binds_ipv4_so_the_railway_healthcheck_can_reach_it() -> None:
    """`0.0.0.0`, not `::` — and this reverses what this test used to assert.

    Two measured facts, both from a real Railway deploy:

    - Railway's healthcheck connects over IPv4 (observed source 100.64.0.2).
    - asyncio sets IPV6_V6ONLY on every AF_INET6 socket it opens, so a uvicorn
      bound to `::` refuses IPv4 outright. Every healthcheck attempt failed with
      "service unavailable" and no request ever reached the application.

    The old premise — that the private network is IPv6-only, so `::` is the only
    reachable bind — stopped being true for environments created after
    2025-10-16, which resolve `*.railway.internal` to both an A and an AAAA
    record. An IPv4 listener is reachable by siblings AND by the healthcheck;
    an IPv6-only one can never pass a healthcheck.

    `web` is deliberately left on `::`: it is a Node server, and Node binds
    dual-stack, so it accepts both. This asymmetry is a property of the
    runtimes, not an inconsistency.
    """
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "0.0.0.0" in compose["services"]["api"]["command"]
    dockerfile = (API_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "--host 0.0.0.0" in dockerfile
    assert "--host ::" not in dockerfile

    fileserver = (API_DIR / "src" / "agent" / "fileserver.py").read_text(encoding="utf-8")
    assert 'host="0.0.0.0"' in fileserver, "the worker's file server must bind IPv4 too"


def test_web_build_receives_the_api_proxy_target() -> None:
    """`next.config.ts` bakes `rewrites()` at build time, so ARG is load-bearing.

    Railway passes a service variable into a Docker build only if the Dockerfile
    declares it with `ARG`. Without this the build silently falls back to the
    compose default `http://api:8000`, ships it to production, and every
    /api/v1 call through the rewrite dies with ENOTFOUND.
    """
    dockerfile = (WEB_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG API_INTERNAL_URL" in dockerfile
    assert "ENV API_INTERNAL_URL=$API_INTERNAL_URL" in dockerfile


def test_no_absolute_api_url_reaches_the_browser() -> None:
    """The frontend calls relative paths only (PRD §13.1). NEXT_PUBLIC_API_URL is a bug."""
    offences = [
        str(path.relative_to(REPO_ROOT))
        for path in WEB_DIR.rglob("*")
        if path.is_file()
        and "node_modules" not in path.parts
        and ".next" not in path.parts
        and "NEXT_PUBLIC_API_URL" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offences, f"NEXT_PUBLIC_API_URL found in {offences}"


def test_production_is_the_last_dockerfile_stage() -> None:
    """Railway builds with no `--target`, so the last stage is what ships.

    The `dev` stage exists for the integration suite and carries pytest and
    mypy. If it ever ends up last, those reach production.
    """
    stages = re.findall(
        r"^FROM\s+\S+\s+AS\s+(\S+)",
        (API_DIR / "Dockerfile").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert stages[-1] != "dev", f"`dev` must not be the final stage; stages are {stages}"
    assert "dev" in stages, "the integration suite's image stage is missing"


def test_the_test_service_is_profiled_and_uses_its_own_database() -> None:
    """`make up` must never start it, and it must never point at the dev database."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["test"]
    assert service["profiles"] == ["test"]
    assert service["build"]["target"] == "dev"
    assert service["environment"]["DATABASE_URL"].endswith("/agent_test")
    assert service["environment"]["REDIS_URL"].endswith("/15")


def test_every_source_variable_reaches_the_container_and_the_example_file() -> None:
    """The three lists that must agree, or a key is set and never arrives.

    `KIND_SPECS` says which variables a source reads. Compose's `x-api-env` is
    an allow-list: a variable absent from it never reaches the container,
    however carefully it was set in `.env`. And `.env.example` is what an
    operator actually copies.

    All three drifting apart is silent by construction — the source just reports
    itself unconfigured, which reads as "the operator forgot" rather than "we
    never passed it through". This is the only place that says otherwise.
    """
    from agent.credential_kinds import KIND_SPECS

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    for service in ("api", "worker"):
        passed = set(compose["services"][service]["environment"])
        for spec in KIND_SPECS.values():
            missing = set(spec.env_vars) - passed
            assert not missing, f"{service} never receives {sorted(missing)}"

    for spec in KIND_SPECS.values():
        for name in spec.env_vars:
            assert re.search(rf"^#?\s*{name}=", example, re.MULTILINE), (
                f"{name} is not in .env.example, so nobody copying it will set it"
            )


def test_the_test_service_blanks_every_source_key() -> None:
    """A developer's own `.env` must not leak into the suite.

    `<<: *api-env` pulls whatever is set locally, and a suite that can see a
    real key stops testing the unconfigured path — which is most of what
    `test_connections_api` is for.
    """
    from agent.credential_kinds import KIND_SPECS

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["test"]["environment"]
    for spec in KIND_SPECS.values():
        for name in spec.env_vars:
            assert environment.get(name) == "", f"the test service inherits {name}"


# --- P2: the embedding model ---------------------------------------------


def test_both_images_bake_the_embedding_model() -> None:
    """fastembed must not download 130MB on the first search request.

    Its default cache is under `tempfile.gettempdir()`, and a Railway container
    filesystem is wiped on every deploy (PRD §5.2). Without a build-time
    download and a cache path outside the default, the first request that
    touches search after each deploy pays a cold download — and fails outright
    if HuggingFace is unreachable.
    """
    for path in (API_DIR / "Dockerfile", WORKER_DOCKERFILE):
        dockerfile = path.read_text(encoding="utf-8")
        assert "FASTEMBED_CACHE_PATH" in dockerfile, f"{path} does not pin the model cache"
        assert "TextEmbedding(" in dockerfile, f"{path} does not pre-download the model"


def test_the_baked_model_is_the_one_the_schema_was_sized_for() -> None:
    """The image, the default setting and the column width are one decision."""
    from agent.config import Settings
    from agent.db.models import EMBEDDING_DIM

    settings = Settings(app_encryption_key="dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE=")
    assert settings.embedding_model == "BAAI/bge-small-en-v1.5"
    assert EMBEDDING_DIM == 384
    for path in (API_DIR / "Dockerfile", WORKER_DOCKERFILE):
        dockerfile = path.read_text(encoding="utf-8")
        assert settings.embedding_model in dockerfile, f"{path} bakes a different model"


def test_the_two_python_services_do_not_share_a_root_directory() -> None:
    """`api` and `worker` must each own the `Dockerfile` of a different root.

    Railway picks the build file by composing Root Directory + `Dockerfile` and,
    on this project, honours nothing else — not the `RAILWAY_DOCKERFILE_PATH`
    service variable (tried in all three path forms) and not the service's own
    Dockerfile Path build setting, even on a service configured before its first
    build ever ran. Two services rooted at the same directory therefore build the
    SAME image, silently: the worker came up running `uvicorn` instead of `arq`
    and Railway still reported the deploy a success.

    So the worker's Dockerfile lives at the repo root and the api's stays in
    `apps/api`. If someone moves the worker's back beside the api's, this fails.
    """
    assert WORKER_DOCKERFILE.is_file(), "the worker's Dockerfile must be at the repo root"
    assert not (API_DIR / "Dockerfile.worker").exists(), (
        "apps/api/Dockerfile.worker is back — Railway cannot select it, and the worker "
        "will silently build apps/api/Dockerfile and run uvicorn instead of arq"
    )

    worker = WORKER_DOCKERFILE.read_text(encoding="utf-8")
    assert "playwright" in worker.lower(), "the repo-root Dockerfile is not the worker's"
    assert 'CMD ["arq"' in worker, "the worker image must start arq"

    api = (API_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "uvicorn" in api, "apps/api/Dockerfile must still be the api image"


def test_the_worker_build_context_is_the_repo_root() -> None:
    """Its COPYs must be repo-root relative, and compose must agree with Railway."""
    worker = WORKER_DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY apps/api/pyproject.toml apps/api/uv.lock" in worker
    assert "COPY apps/api/ ./" in worker

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    build = compose["services"]["worker"]["build"]
    assert build["context"] == ".", "compose must build the worker from the repo root too"
    assert build["dockerfile"] == "Dockerfile"


def test_the_repo_root_has_a_dockerignore() -> None:
    """Docker reads the `.dockerignore` beside the CONTEXT root.

    Moving the worker's context to the repo root silently stopped
    `apps/api/.dockerignore` applying to it. Without a root one the build uploads
    `apps/web/node_modules` and `.next`.
    """
    ignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in ("apps/web/", "**/node_modules/", "**/.venv/", "apps/api/tests/"):
        assert pattern in ignore, f".dockerignore is missing {pattern}"
