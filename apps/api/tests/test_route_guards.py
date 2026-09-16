"""The CI guard runs in the test suite too, so a missing `require()` fails locally first."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = API_ROOT / "scripts" / "check_route_guards.py"


def test_every_route_declares_a_permission() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=API_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_public_allowlist_is_exactly_the_prd_list_plus_csrf() -> None:
    """PRD §14 names four public routes; `/auth/csrf` and the invite preview are the rest."""
    sys.path.insert(0, str(API_ROOT / "scripts"))
    from check_route_guards import PUBLIC_ROUTES

    expected = {
        ("GET", "/health"),
        ("GET", "/auth/csrf"),
        ("POST", "/auth/bootstrap"),
        ("POST", "/auth/login"),
        ("GET", "/invites/{token}"),
        ("POST", "/invites/{token}/accept"),
    }
    assert set(PUBLIC_ROUTES) == expected
