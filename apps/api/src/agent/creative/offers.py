"""OfferBinding resolution against the run's offer snapshot (Stage 04 PRD §12.2, law 35).

The model never writes a number or a date in an offer. A promotion or price
asset *binds* each figure to an `OfferRecord` field, and this module renders
the figure from the record — so what an ad says is what the offer data says,
and release (S4-P16) can re-resolve the same binding against the live row and
refuse on drift.

* **Identity.** `OfferRecord` has no id, and the snapshot is re-read from
  `offer_record` evidence at every run start. An offer is its natural key —
  product set, SKU and market — so `record_id` is a UUIDv5 of that key: the
  same offer keeps its id when its price changes, which is exactly the drift
  release looks for.
* **Usable.** Fresh (`observed_at` within `offer_max_age_days` of the run's
  start) and live at it (inside its effective window, not ended). An offer
  with no `observed_at` is stale: its age is unknown, and law 35 skips what it
  cannot vouch for rather than guessing. One observed without a timezone
  (`csv_ingest` dates carry none) is aged as UTC — a zone cannot move a
  seven-day window. Of several observations of one offer, the latest wins.
* **Rendering.** In code, from the record, as Stage 03's offer matchers
  derive the same figures (`guardrails/matchers/offers.py`): `percent_off`
  needs a documented `reference_price` and is floored to a whole percent, so
  an ad never claims more than the offer gives; `money_off` is the saving on
  the reference price, else the list price; money is exact decimal to the
  cent; a date is ISO 8601 and must carry its timezone — a deadline without
  one means a different moment to every reader.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Final

from agent.schemas.creative_brief import OfferBinding
from agent.schemas.extras import OFFER_REFS
from agent.schemas.guardrails import OfferRecord

#: The UUIDv5 namespace of offer identities. Fixed forever: changing it would
#: re-key every binding ever released.
OFFER_NAMESPACE: Final = uuid.UUID("0d4c3f7e-5b8a-4c1e-9f26-7a3b1e6d8c42")

#: `CreativeInput`'s market when the plan names none: every offer is the
#: project's (`nodes/creative/_ad_groups.UNKNOWN_MARKET`).
ANY_MARKET: Final = "*"

_CENT: Final = Decimal("0.01")


class OfferBindingError(ValueError):
    """A field the record cannot honestly render — never bound, never guessed."""


def record_id(record: OfferRecord) -> uuid.UUID:
    """The offer's identity: its product set, SKU and market."""
    key = "\x1f".join((record.product_set, record.sku, record.market.casefold()))
    return uuid.uuid5(OFFER_NAMESPACE, key)


@dataclass(frozen=True, slots=True)
class Usable:
    usable: list[OfferRecord] = field(default_factory=list)
    stale: list[OfferRecord] = field(default_factory=list)
    not_live: list[OfferRecord] = field(default_factory=list)


def _live(record: OfferRecord, now: datetime) -> bool:
    start, stop, ends = (
        _aware(value) for value in (record.effective_from, record.effective_to, record.ends_at)
    )
    if start is not None and start > now:
        return False
    if stop is not None and stop <= now:
        return False
    return ends is None or ends > now


def usable(records: Iterable[OfferRecord], *, now: datetime, max_age_days: int) -> Usable:
    """The snapshot's offers split into usable, stale and not live at `now`."""
    horizon = now - timedelta(days=max_age_days)
    latest: dict[uuid.UUID, OfferRecord] = {}
    for record in records:
        key = record_id(record)
        seen = latest.get(key)
        if seen is None or _order(record) > _order(seen):
            latest[key] = record
    found = Usable()
    for record in latest.values():
        observed = _aware(record.observed_at)
        if observed is None or observed < horizon:
            found.stale.append(record)
        elif not _live(record, now):
            found.not_live.append(record)
        else:
            found.usable.append(record)
    return found


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _order(record: OfferRecord) -> tuple[bool, float]:
    """Latest first; an undated observation is older than any dated one."""
    observed = _aware(record.observed_at)
    return (observed is not None, observed.timestamp() if observed else 0.0)


def in_market(records: Sequence[OfferRecord], market: str) -> list[OfferRecord]:
    """The offers of `market` (case-insensitive); all of them when the plan names none."""
    if market == ANY_MARKET:
        return list(records)
    return [record for record in records if record.market.casefold() == market.casefold()]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _money(value: float | Decimal) -> str:
    return str(Decimal(str(value)).quantize(_CENT))


def _date(value: datetime | None, name: str) -> str:
    if value is None:
        raise OfferBindingError(f"the offer has no {name}")
    if value.tzinfo is None:
        raise OfferBindingError(f"{name} has no timezone, so the date means a different moment")
    return value.isoformat()


def _percent_off(record: OfferRecord) -> str:
    reference = record.reference_price
    if reference is None or reference <= 0:
        raise OfferBindingError("a percentage discount needs a documented reference_price")
    saving = Decimal(str(reference)) - Decimal(str(record.current_price))
    percent = (saving * 100 / Decimal(str(reference))).quantize(Decimal(1), rounding=ROUND_FLOOR)
    if percent < 1:
        raise OfferBindingError("the offer saves less than one percent")
    return str(percent)


def _money_off(record: OfferRecord) -> str:
    base = record.reference_price if record.reference_price is not None else record.list_price
    saving = Decimal(str(base)) - Decimal(str(record.current_price))
    if saving <= 0:
        raise OfferBindingError("the offer saves nothing")
    return _money(saving)


def _present(value: float | None, name: str) -> str:
    if value is None:
        raise OfferBindingError(f"the offer has no {name}")
    return _money(value)


_RENDER: Final[Mapping[str, Callable[[OfferRecord], str]]] = {
    "current_price": lambda r: _money(r.current_price),
    "list_price": lambda r: _money(r.list_price),
    "reference_price": lambda r: _present(r.reference_price, "reference_price"),
    "currency": lambda r: r.currency.strip().upper(),
    "effective_from": lambda r: _date(r.effective_from, "effective_from"),
    "effective_to": lambda r: _date(r.effective_to, "effective_to"),
    "ends_at": lambda r: _date(r.ends_at, "ends_at"),
    "percent_off": _percent_off,
    "money_off": _money_off,
}
assert set(_RENDER) == OFFER_REFS, "every OfferRef renders, and nothing else does"


def resolve(record: OfferRecord, fields: Mapping[str, str]) -> dict[str, str]:
    """`asset field -> rendered value` for every reference in `fields`."""
    resolved: dict[str, str] = {}
    for name, ref in fields.items():
        render = _RENDER.get(ref)
        if render is None:
            raise OfferBindingError(f"{name} names {ref!r}, which is not an offer field")
        resolved[name] = render(record)
    return resolved


def bind(record: OfferRecord, fields: Mapping[str, str]) -> OfferBinding:
    """The binding, with every value rendered from `record`. Raises `OfferBindingError`."""
    return OfferBinding(
        offer_record_id=record_id(record),
        sku_or_set=record.sku,
        fields=dict(fields),
        resolved=resolve(record, fields),
    )


def promotion_fields(record: OfferRecord) -> dict[str, str] | None:
    """What a promotion of `record` binds, or None when the offer saves nothing.

    A documented reference price makes it a percentage (Stage 03 checks a
    percentage against exactly that); otherwise a saving on the list price is
    an amount off.
    """
    try:
        _percent_off(record)
        kind = "percent_off"
    except OfferBindingError:
        try:
            _money_off(record)
            kind = "money_off"
        except OfferBindingError:
            return None
    fields = {kind: kind, "currency": "currency"}
    if record.effective_from is not None:
        fields["start"] = "effective_from"
    if record.ends_at is not None:
        fields["end"] = "ends_at"
    elif record.effective_to is not None:
        fields["end"] = "effective_to"
    return fields


def price_fields(record: OfferRecord) -> dict[str, str]:
    """What a price item binds: the record's current price, in its currency.

    The same for every record; `record` is taken so the call reads like
    `promotion_fields`, the one that differs.
    """
    del record
    return {"price": "current_price", "currency": "currency"}
