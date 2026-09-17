"""HTTP routers. Everything is mounted under /api/v1."""

from __future__ import annotations

#: Defined here rather than in `main` because a route that has to build its own
#: absolute URL — an OAuth redirect_uri, which Google compares byte for byte —
#: cannot import `main` without a cycle.
API_PREFIX = "/api/v1"
