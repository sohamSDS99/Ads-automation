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

RAILWAY_FILES = {
    API_DIR / "railway.json": API_DIR,
    API_DIR / "railway.worker.json": API_DIR,
    WEB_DIR / "railway.json": WEB_DIR,
}


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
    config = json.loads((API_DIR / "railway.worker.json").read_text(encoding="utf-8"))
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


def test_api_binds_ipv6() -> None:
    """Railway's private network is IPv6-only; a service on 0.0.0.0 is unreachable."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "::" in compose["services"]["api"]["command"]
    assert "0.0.0.0" not in " ".join(compose["services"]["api"]["command"])
    dockerfile = (API_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "--host ::" in dockerfile


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


# --- P2: the embedding model ---------------------------------------------


def test_both_images_bake_the_embedding_model() -> None:
    """fastembed must not download 130MB on the first search request.

    Its default cache is under `tempfile.gettempdir()`, and a Railway container
    filesystem is wiped on every deploy (PRD §5.2). Without a build-time
    download and a cache path outside the default, the first request that
    touches search after each deploy pays a cold download — and fails outright
    if HuggingFace is unreachable.
    """
    for name in ("Dockerfile", "Dockerfile.worker"):
        dockerfile = (API_DIR / name).read_text(encoding="utf-8")
        assert "FASTEMBED_CACHE_PATH" in dockerfile, f"{name} does not pin the model cache"
        assert "TextEmbedding(" in dockerfile, f"{name} does not pre-download the model"


def test_the_baked_model_is_the_one_the_schema_was_sized_for() -> None:
    """The image, the default setting and the column width are one decision."""
    from agent.config import Settings
    from agent.db.models import EMBEDDING_DIM

    settings = Settings(app_encryption_key="dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE=")
    assert settings.embedding_model == "BAAI/bge-small-en-v1.5"
    assert EMBEDDING_DIM == 384
    for name in ("Dockerfile", "Dockerfile.worker"):
        dockerfile = (API_DIR / name).read_text(encoding="utf-8")
        assert settings.embedding_model in dockerfile, f"{name} bakes a different model"
