"""The deploy surface: three valid railway.json files, and no api/worker ingress."""

from __future__ import annotations

import json
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
