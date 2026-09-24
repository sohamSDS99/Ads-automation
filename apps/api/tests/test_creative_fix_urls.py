"""Every `fix_url` Stage 04's eligibility can emit names a screen that exists.

A blocker's link is the only way out of a locked stage (Stage 04 PRD §15.1
rule 1: the landing names the blocker "with a link to the stage that fixes
it"). A link to a route the web app does not have is worse than no link:
Next prefetches every in-viewport `<Link>`, so it 404s in the console of
everyone who merely *looks* at the landing, and then again for whoever clicks.

The list of real routes is derived from `apps/web/app` rather than written
down here, so a page that moves or a new blocker that points nowhere fails
this test without anybody having to remember to update it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_APP = REPO_ROOT / "apps" / "web" / "app"
ROUTES = REPO_ROOT / "apps" / "api" / "src" / "agent" / "api" / "routes_creative.py"


def web_routes() -> list[re.Pattern[str]]:
    """Every page the web app serves, as a regex over a concrete path."""
    patterns = []
    for page in WEB_APP.rglob("page.tsx"):
        parts = [
            part
            for part in page.parent.relative_to(WEB_APP).parts
            # A route group like `(app)` is not part of the URL.
            if not (part.startswith("(") and part.endswith(")"))
        ]
        segments = [
            "[^/]+" if part.startswith("[") and part.endswith("]") else re.escape(part)
            for part in parts
        ]
        patterns.append(re.compile("^/" + "/".join(segments) + "$"))
    return patterns


def emitted_fix_urls() -> list[tuple[int, str]]:
    """Each `fix_url=` in routes_creative.py, with interpolations as a placeholder segment.

    `{home}` is the project's home, `/projects/{pid}`; any other interpolation
    is one path parameter.
    """
    found = []
    for node in ast.walk(ast.parse(ROUTES.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.keyword) or node.arg != "fix_url":
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.append((node.value.lineno, value.value))
        elif isinstance(value, ast.JoinedStr):
            rendered = ""
            for piece in value.values:
                if isinstance(piece, ast.Constant):
                    rendered += str(piece.value)
                elif isinstance(piece, ast.FormattedValue):
                    is_home = isinstance(piece.value, ast.Name) and piece.value.id == "home"
                    rendered += "/projects/PROJECT" if is_home else "PARAM"
            found.append((node.value.lineno, rendered))
    return found


def test_the_web_app_is_where_this_test_thinks_it_is() -> None:
    # A wrong path would make the next test vacuous — no routes, so no match,
    # so every URL "fails" — or, worse, a glob that finds nothing and asserts
    # over an empty list.
    assert (WEB_APP / "(app)" / "projects" / "[id]" / "plan" / "page.tsx").is_file()
    assert len(web_routes()) > 20
    assert len(emitted_fix_urls()) >= 15


def test_every_eligibility_fix_url_names_an_existing_screen() -> None:
    routes = web_routes()
    dead = [
        f"routes_creative.py:{line} -> {url}"
        for line, url in emitted_fix_urls()
        if not any(route.match(url.split("?")[0].split("#")[0]) for route in routes)
    ]
    assert dead == [], "fix_url points at a page the web app does not have:\n" + "\n".join(dead)
