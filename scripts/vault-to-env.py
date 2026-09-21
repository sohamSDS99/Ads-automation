#!/usr/bin/env python3
"""Print the retired vault's secrets as the `.env` lines that replace them.

Every credential is read from the environment now. The `credential` table is
left on disk by migration 0012 for one reason: its rows are AES-256-GCM
ciphertext that no endpoint ever returned, so for any workspace that connected
a source through the old screen and never wrote the same value into a file,
that row is the only copy in existence. A Google Ads refresh token is the case
that matters — the retired consent flow minted it straight into the vault and
showed nobody.

This reads those rows, decrypts them with the deployment's own
`APP_ENCRYPTION_KEY`, and prints them as `NAME=value` lines. It writes nothing,
changes nothing, and touches no workspace state.

    # inside the api container, where DATABASE_URL and APP_ENCRYPTION_KEY are set
    docker compose exec -T api python /app/scripts/vault-to-env.py
    railway run --service api python scripts/vault-to-env.py

Then paste the lines into `.env` (or set them as Railway variables), restart the
API, and switch each source on under Settings → Connections. Once every
deployment has done that, the `credential` table can be dropped in a migration
of its own.

Output is secrets in plaintext, by design and by necessity. Run it where you
would run `psql`, not where the log is shipped somewhere.
"""

from __future__ import annotations

import asyncio
import json
import sys

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, "/app/src")

from agent.config import get_settings  # noqa: E402
from agent.credential_kinds import KIND_SPECS  # noqa: E402
from agent.crypto import DecryptionError, decrypt_str  # noqa: E402
from agent.db.models import CredentialKind  # noqa: E402


def credential_aad(credential_id: object) -> bytes:
    """The AAD the retired vault sealed each row with: its own id.

    Kept here rather than in `agent.credentials`, which no longer knows the
    vault exists. This file is the last reader of that format and should carry
    its own copy of the one detail it needs.
    """
    return str(credential_id).encode("utf-8")


def unseal(kind: CredentialKind, secret: str) -> dict[str, str]:
    """The sealed string back as field-name → value.

    The old vault sealed a multi-field kind as a JSON object and a single-field
    kind as the bare value, and the JSON branch is tried for both: a real key
    never parses as a JSON object, so the fallback still catches every
    single-field credential — including one written before a kind was narrowed
    to one field.
    """
    try:
        parsed = json.loads(secret)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return {str(key): str(value) for key, value in parsed.items() if value is not None}
    return {KIND_SPECS[kind].fields[0].name: secret}


async def main() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.async_database_url)
    lines: list[str] = []
    skipped: list[str] = []

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT id, kind, scope, ciphertext, nonce, created_at "
                    "FROM credential ORDER BY kind, created_at DESC"
                )
            )
        ).all()

    seen: set[str] = set()
    for row in rows:
        try:
            kind = CredentialKind(row.kind)
        except ValueError:
            skipped.append(f"{row.kind} (this build has no such kind)")
            continue
        spec = KIND_SPECS.get(kind)
        if spec is None:
            skipped.append(f"{row.kind} (not a connectable source)")
            continue
        try:
            secret = decrypt_str(row.ciphertext, row.nonce, aad=credential_aad(row.id))
        except DecryptionError:
            skipped.append(f"{row.kind} {row.id} (wrong APP_ENCRYPTION_KEY)")
            continue

        values = unseal(kind, secret)
        for field in spec.fields:
            value = values.get(field.name)
            # Newest row per kind wins, which is the one the old resolver would
            # have reached for at workspace scope. An older row for the same
            # kind is a replaced key, and printing both would leave whoever
            # pastes this to guess which is live.
            if not value or field.env_var in seen:
                continue
            seen.add(field.env_var)
            lines.append(f"{field.env_var}={value}")

    print("# Recovered from the retired credential vault.")
    print("# Paste into .env (or set as Railway variables) and restart the API.")
    if not lines:
        print("# nothing to recover — the vault holds no readable rows")
    for line in lines:
        print(line)
    for note in skipped:
        print(f"# skipped: {note}", file=sys.stderr)

    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
