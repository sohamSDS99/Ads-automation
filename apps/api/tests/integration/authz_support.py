"""What "a refused call writes nothing" means to the authz matrices (PRD §19.1, CC10).

Every table in the schema, read row by row — not a hand-picked list of models.
A hand-picked list is how the Stage 01 matrix came to watch five tables while
Stage 04 added ten that a forbidden call could write to: nothing failed, the
guarantee just quietly narrowed. Reading the live catalogue means a table a
later stage adds is watched the day its migration lands.

`STAGE04_TABLES` is derived the same way, from the migrations Stage 04 shipped,
so a test can say "every Stage 04 table is watched" without restating them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

#: Stage 04's first migration (0019 creates its enums, 0020 its tables).
STAGE04_FIRST_REVISION = "0019"


def _revision(tree: ast.Module) -> str | None:
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        value = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and target.id == "revision"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def tables_created_by(tree: ast.Module) -> list[str]:
    """Every `op.create_table("<name>", ...)` in one migration."""
    return [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_table"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]


def stage04_tables() -> tuple[str, ...]:
    """The tables Stage 04's migrations created, read out of the migrations."""
    created: set[str] = set()
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text())
        revision = _revision(tree)
        if revision is not None and revision >= STAGE04_FIRST_REVISION:
            created.update(tables_created_by(tree))
    return tuple(sorted(created))


STAGE04_TABLES: tuple[str, ...] = stage04_tables()


async def table_fingerprints(db: AsyncSession) -> dict[str, tuple[int, str]]:
    """`{table: (row count, md5 of every row's text)}` for every table there is.

    The digest covers every column of every row, so an UPDATE that leaves the
    count alone — a status flipped, a `retired_at` stamped, a draft saved —
    changes it as surely as an INSERT does. One statement, so the before and
    after pictures are each a single consistent read.
    """
    names = (
        (
            await db.execute(
                sa.text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                    "AND tablename <> 'alembic_version' ORDER BY tablename"
                )
            )
        )
        .scalars()
        .all()
    )
    union = " UNION ALL ".join(
        f"SELECT '{name}' AS t, count(*) AS n, "  # noqa: S608 — names from pg_tables
        f"md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), '')) AS h "
        f'FROM "{name}" r'
        for name in names
    )
    rows = await db.execute(sa.text(union))
    return {str(t): (int(n), str(h)) for t, n, h in rows.all()}
