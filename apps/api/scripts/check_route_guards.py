#!/usr/bin/env python
"""CI guard: every route declares a permission, or the build fails.

PRD §6.1, Authorization 1: "Every route declares one — a route without it fails
a CI check that walks the router modules." This is that check.

It proves three things:

1. Every `APIRoute` in every `agent.api.routes_*` module either carries a
   `require(Permission)` dependency or is on the public allowlist below.
2. Nothing is on the allowlist that is not actually a route, so the list cannot
   rot into a blanket exemption after a rename.
3. Every router module is actually mounted by `create_app`. A guarded route
   nobody included is not a security hole, but it is a silent feature outage,
   and this is the cheapest place to catch it.

Run: `uv run python scripts/check_route_guards.py`
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# Importing the app reads configuration. The values are irrelevant to route
# inspection — only their presence is — so supply throwaways rather than
# requiring a configured environment to lint routes.
os.environ.setdefault("APP_ENCRYPTION_KEY", "cm91dGUtZ3VhcmQtY2hlY2sta2V5LTMyLWJ5dGVzISE=")
os.environ.setdefault("DATABASE_URL", "postgresql://check:check@localhost:5432/check")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from fastapi import APIRouter  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

import agent.api as api_package  # noqa: E402

#: The only routes that may be reached without a session, straight from PRD §14.
#: `/auth/csrf` is the one addition: the double-submit cookie has to exist before
#: an unauthenticated browser can POST to `/auth/login`, and it returns nothing
#: but a random token.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/auth/csrf"),
        ("POST", "/auth/bootstrap"),
        ("POST", "/auth/login"),
        ("GET", "/invites/{token}"),
        ("POST", "/invites/{token}/accept"),
    }
)


def route_modules() -> list[str]:
    return sorted(
        f"agent.api.{module.name}"
        for module in pkgutil.iter_modules(api_package.__path__)
        if module.name.startswith("routes_")
    )


def declared_permission(route: APIRoute) -> str | None:
    """Find a `require(Permission)` anywhere in the route's dependency tree."""
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        for sub in dependant.dependencies:
            permission = getattr(sub.call, "__ara_permission__", None)
            if permission is not None:
                return str(permission)
            stack.append(sub)
    return None


def main() -> int:
    failures: list[str] = []
    seen: set[tuple[str, str]] = set()
    guarded = 0

    for module_name in route_modules():
        module = importlib.import_module(module_name)
        router = getattr(module, "router", None)
        if not isinstance(router, APIRouter):
            failures.append(f"{module_name}: no module-level `router: APIRouter`")
            continue

        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                key = (method, route.path)
                seen.add(key)
                permission = declared_permission(route)
                if permission is not None:
                    guarded += 1
                    if key in PUBLIC_ROUTES:
                        failures.append(
                            f"{method} {route.path} is on the public allowlist but declares "
                            f"require({permission}). Remove it from PUBLIC_ROUTES."
                        )
                elif key not in PUBLIC_ROUTES:
                    failures.append(
                        f"{method} {route.path} ({module_name}) declares no require(Permission) "
                        "and is not a public route."
                    )

    for key in sorted(PUBLIC_ROUTES - seen):
        failures.append(f"{key[0]} {key[1]} is allowlisted as public but no such route exists.")

    from agent.main import API_PREFIX, create_app

    mounted = {
        (method, path.removeprefix(API_PREFIX))
        for path, operations in create_app().openapi()["paths"].items()
        for method in (m.upper() for m in operations)
    }
    # OpenAPI prints a converter-typed parameter bare: `{path:path}` is `{path}`.
    for key in sorted(k for k in seen if (k[0], re.sub(r":\w+}", "}", k[1])) not in mounted):
        failures.append(f"{key[0]} {key[1]} is defined but never included by create_app().")

    if failures:
        print("Route guard check FAILED:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"Route guard check passed: {guarded} guarded, "
        f"{len(PUBLIC_ROUTES)} public, {len(seen)} total."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
