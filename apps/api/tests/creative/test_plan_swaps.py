"""`clearance.plan_swaps` — what refusing a set of exceptions does to their
assets, without doing it (S4-P22).

One planner now sits behind the write (`swap_to_fallbacks`) and both previews
(H3's `if_rejected`, the withdraw confirmation's counts), so its status
overlay must move exactly as the write moves statuses. These cases are the
ones the overlay exists for; the integration suite never reaches them (a
mutation that removed the overlay left it green).
"""

from __future__ import annotations

import uuid
from typing import Any

from agent.creative.clearance import Swap, plan_swaps
from agent.db.models import CreativeAsset, CreativeAssetKind, CreativeAssetStatus, CreativeException

RUN = uuid.uuid4()


def _asset(status: CreativeAssetStatus, **slot: Any) -> CreativeAsset:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "creative_run_id": RUN,
        "campaign_ref": "Search - SDS",
        "ad_group_ref": "SDS software",
        "variant": "A",
        "kind": CreativeAssetKind.DESCRIPTION,
        "surface": "rsa_description",
        "status": status,
    }
    fields.update(slot)
    return CreativeAsset(**fields)


def _exception(tied: list[CreativeAsset], fallbacks: list[CreativeAsset]) -> CreativeException:
    return CreativeException(
        id=uuid.uuid4(),
        asset_ids=[a.id for a in tied],
        fallback_asset_ids=[a.id for a in fallbacks],
    )


def _by_id(*assets: CreativeAsset) -> dict[uuid.UUID, CreativeAsset]:
    return {a.id: a for a in assets}


def test_a_carried_asset_swaps_to_its_same_slot_reserve_and_a_draft_just_drops() -> None:
    carried = _asset(CreativeAssetStatus.LINTED)
    draft = _asset(CreativeAssetStatus.DRAFT)
    reserve = _asset(CreativeAssetStatus.RESERVE)
    other_slot = _asset(CreativeAssetStatus.RESERVE, ad_group_ref="Another group")
    row = _exception([carried, draft], [other_slot, reserve])

    swaps = plan_swaps([row], _by_id(carried, draft, reserve, other_slot))

    assert swaps == [Swap(out=carried.id, into=reserve.id), Swap(out=draft.id, into=None)]


def test_an_asset_tied_to_two_exceptions_drops_once() -> None:
    tied = _asset(CreativeAssetStatus.LINTED)
    reserve = _asset(CreativeAssetStatus.RESERVE)
    first = _exception([tied], [reserve])
    second = _exception([tied], [reserve])

    swaps = plan_swaps([first, second], _by_id(tied, reserve))

    assert swaps == [Swap(out=tied.id, into=reserve.id)]


def test_a_fallback_fills_one_slot_only() -> None:
    one = _asset(CreativeAssetStatus.LINTED)
    two = _asset(CreativeAssetStatus.APPROVED)
    reserve = _asset(CreativeAssetStatus.RESERVE)
    row = _exception([one, two], [reserve])

    swaps = plan_swaps([row], _by_id(one, two, reserve))

    assert swaps == [Swap(out=one.id, into=reserve.id), Swap(out=two.id, into=None)]


def test_a_used_fallback_that_a_later_exception_ties_drops_and_seeks_its_own() -> None:
    tied = _asset(CreativeAssetStatus.LINTED)
    fallback = _asset(CreativeAssetStatus.RESERVE)
    last = _asset(CreativeAssetStatus.RESERVE)
    first = _exception([tied], [fallback])
    # Once `fallback` carries the slot it is carried, so refusing it too drops it
    # and puts the next reserve in — exactly what the write's statuses do.
    second = _exception([fallback], [last])

    swaps = plan_swaps([first, second], _by_id(tied, fallback, last))

    assert swaps == [Swap(out=tied.id, into=fallback.id), Swap(out=fallback.id, into=last.id)]


def test_an_already_dropped_asset_and_an_unknown_one_are_skipped() -> None:
    dropped = _asset(CreativeAssetStatus.DROPPED)
    row = _exception([dropped], [])
    row.asset_ids = [*row.asset_ids, uuid.uuid4()]

    assert plan_swaps([row], _by_id(dropped)) == []


def test_planning_moves_no_real_status() -> None:
    tied = _asset(CreativeAssetStatus.LINTED)
    reserve = _asset(CreativeAssetStatus.RESERVE)

    plan_swaps([_exception([tied], [reserve])], _by_id(tied, reserve))

    assert (tied.status, reserve.status) == (
        CreativeAssetStatus.LINTED,
        CreativeAssetStatus.RESERVE,
    )
