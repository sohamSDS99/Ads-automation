"""The IANA database has to be present wherever a schedule is evaluated.

This is not a hypothetical. The worker image is built on the Playwright
base — Ubuntu jammy with no `/usr/share/zoneinfo` — and `zoneinfo` falls back
to the `tzdata` PyPI package when the system database is missing. Without that
package installed, `ZoneInfo("UTC")` raises, `next_at_for` raises `CronError`,
and the poller does the one thing it is written to do with an expression it
cannot parse: disables the row.

Every schedule in the workspace, silently switched off on the first tick after
deploy, with one warning each in a log nobody is reading. `verify-p8.sh` caught
it; this file is what stops it coming back with the next base-image change.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent.scheduling import cron

#: UTC is the default for every `Schedule` row, and the others are zones this
#: workspace actually operates in. A base image that carries none of them is
#: not a base image this product can run the scheduler on.
REQUIRED_ZONES = ("UTC", "Europe/Copenhagen", "Europe/London", "America/New_York", "Asia/Kolkata")


@pytest.mark.parametrize("zone", REQUIRED_ZONES)
def test_the_zone_resolves(zone: str) -> None:
    assert cron.load_timezone(zone) is not None


@pytest.mark.parametrize("zone", REQUIRED_ZONES)
def test_a_schedule_can_actually_be_evaluated_in_it(zone: str) -> None:
    """The end the poller cares about: a next firing, not merely an importable zone."""
    fired = cron.next_fire(
        cron.parse("*/5 * * * *"), after=datetime(2026, 6, 1, 12, tzinfo=UTC), timezone=zone
    )
    assert fired > datetime(2026, 6, 1, 12, tzinfo=UTC)


def test_the_tzdata_package_is_installed() -> None:
    """Named directly, so the failure says what to add rather than what broke.

    `zoneinfo` prefers the system database and falls back to this package. Both
    being absent is the deployable state that has to be impossible.
    """
    import importlib.util

    assert importlib.util.find_spec("tzdata") is not None, (
        "the `tzdata` package is missing and the base image may ship no "
        "/usr/share/zoneinfo — add tzdata to pyproject dependencies"
    )
