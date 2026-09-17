"""Working out the two setup fields nobody should have to look up.

Wizard step 1 asks for a site to crawl and a list of markets. Both are already
knowable. The site is wherever the domain actually resolves to — one request
answers it, including the `www.` and the country path a person would otherwise
have to remember. The markets are either the countries this business already
sells into, which the CRM export states outright, or the ones its own website
publishes in its `hreflang` links, which is a site declaring its markets in
machine-readable form. Asking someone to type either is asking them to
transcribe something the system can read.

Three rules hold this together.

**Nothing is invented.** Every finding names what was read to produce it, and
finding nothing is a result — reported as such — rather than a gap papered over
with a plausible guess. PRD §16's rule for a missing source is the same rule
here: say so, do not fill it in.

**No model is involved.** Redirects and `hreflang` are facts with one reading,
and PRD §18 law 3 puts that kind of work in Python. A model asked to name a
company's markets will always answer, which is exactly the failure mode.

**The person stays in charge.** Everything here writes a value they can see and
change. `settings["autofill"]` records only that they asked for help.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import sqlalchemy as sa
import structlog
from selectolax.parser import HTMLParser
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import Settings
from agent.db.models import Evidence, Project

log = structlog.get_logger(__name__)

#: The two fields this module can fill. Keyed the way the wizard names them.
AUTOFILL_FIELDS: tuple[str, ...] = ("site_url", "markets")

#: Where the flags live. `settings` already holds `gate_assignees` and
#: `max_creatives`, so a new preference needs no migration.
SETTINGS_KEY = "autofill"

#: The CRM kinds that carry a country per row (`connectors/csv_ingest.py`).
CRM_KINDS: tuple[str, ...] = ("crm_won", "crm_lost")

#: How many markets to propose. A site with forty `hreflang` links is
#: publishing its translation coverage, not its target markets, and a research
#: run sized in forty countries is nobody's intent. What is dropped is reported.
MAX_MARKETS = 6

#: Country → (primary language, currency). Only what the language and currency
#: fields of `Market` need, and deliberately short: a country that is not here
#: is reported as unrecognised rather than given a guessed currency.
COUNTRIES: dict[str, tuple[str, str]] = {
    "AE": ("ar", "AED"),
    "AT": ("de", "EUR"),
    "AU": ("en", "AUD"),
    "BE": ("nl", "EUR"),
    "BG": ("bg", "BGN"),
    "BR": ("pt", "BRL"),
    "CA": ("en", "CAD"),
    "CH": ("de", "CHF"),
    "CN": ("zh", "CNY"),
    "CZ": ("cs", "CZK"),
    "DE": ("de", "EUR"),
    "DK": ("da", "DKK"),
    "EE": ("et", "EUR"),
    "ES": ("es", "EUR"),
    "FI": ("fi", "EUR"),
    "FR": ("fr", "EUR"),
    "GB": ("en", "GBP"),
    "GR": ("el", "EUR"),
    "HK": ("zh", "HKD"),
    "HR": ("hr", "EUR"),
    "HU": ("hu", "HUF"),
    "IE": ("en", "EUR"),
    "IL": ("he", "ILS"),
    "IN": ("en", "INR"),
    "IS": ("is", "ISK"),
    "IT": ("it", "EUR"),
    "JP": ("ja", "JPY"),
    "KR": ("ko", "KRW"),
    "LT": ("lt", "EUR"),
    "LU": ("fr", "EUR"),
    "LV": ("lv", "EUR"),
    "MX": ("es", "MXN"),
    "MY": ("ms", "MYR"),
    "NL": ("nl", "EUR"),
    "NO": ("nb", "NOK"),
    "NZ": ("en", "NZD"),
    "PH": ("en", "PHP"),
    "PL": ("pl", "PLN"),
    "PT": ("pt", "EUR"),
    "RO": ("ro", "RON"),
    "SA": ("ar", "SAR"),
    "SE": ("sv", "SEK"),
    "SG": ("en", "SGD"),
    "SI": ("sl", "EUR"),
    "SK": ("sk", "EUR"),
    "TH": ("th", "THB"),
    "TR": ("tr", "TRY"),
    "TW": ("zh", "TWD"),
    "UA": ("uk", "UAH"),
    "US": ("en", "USD"),
    "VN": ("vi", "VND"),
    "ZA": ("en", "ZAR"),
}

#: The country names a CRM export actually contains. A person exporting deals
#: types "United States", not "US" — and a row whose country cannot be resolved
#: is counted as unresolved rather than dropped silently.
COUNTRY_NAMES: dict[str, str] = {
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "america": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "england": "GB",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
    "spain": "ES",
    "italy": "IT",
    "netherlands": "NL",
    "holland": "NL",
    "belgium": "BE",
    "austria": "AT",
    "switzerland": "CH",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "iceland": "IS",
    "ireland": "IE",
    "poland": "PL",
    "portugal": "PT",
    "czechia": "CZ",
    "czech republic": "CZ",
    "slovakia": "SK",
    "slovenia": "SI",
    "hungary": "HU",
    "romania": "RO",
    "bulgaria": "BG",
    "croatia": "HR",
    "greece": "GR",
    "estonia": "EE",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "canada": "CA",
    "mexico": "MX",
    "brazil": "BR",
    "australia": "AU",
    "new zealand": "NZ",
    "india": "IN",
    "japan": "JP",
    "china": "CN",
    "south korea": "KR",
    "korea": "KR",
    "singapore": "SG",
    "malaysia": "MY",
    "thailand": "TH",
    "vietnam": "VN",
    "philippines": "PH",
    "hong kong": "HK",
    "taiwan": "TW",
    "israel": "IL",
    "turkey": "TR",
    "ukraine": "UA",
    "south africa": "ZA",
    "united arab emirates": "AE",
    "uae": "AE",
    "saudi arabia": "SA",
}

HREFLANG = re.compile(r"^([a-z]{2})(?:[-_]([a-zA-Z]{2}))?$")


@dataclass(frozen=True, slots=True)
class Finding:
    """One field the agent worked out, and what it read to get there.

    `source` is written to be shown: the person is about to accept or reject
    this, and "from your CRM export" and "from your site's language links" are
    different enough claims that they should not both render as "auto".
    """

    field: str
    value: Any
    source: str
    found: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "found": self.found,
        }


def flags(project: Project) -> dict[str, bool]:
    """Which fields this project has asked the agent to keep."""
    raw = (project.settings or {}).get(SETTINGS_KEY) or {}
    return {field: bool(raw.get(field)) for field in AUTOFILL_FIELDS}


def normalise_country(value: Any) -> str | None:
    """A CRM cell to ISO 3166-1 alpha-2, or None if it cannot be read as one."""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 2 and text.upper() in COUNTRIES:
        return text.upper()
    return COUNTRY_NAMES.get(text.casefold())


def market_for(country: str, language: str | None = None) -> dict[str, str] | None:
    """A `Market` for this country, or None when its currency is not known.

    Refusing is the point: `Market` requires a currency, and a wrong one is
    worse than an absent market because every CPC in the report inherits it.
    """
    known = COUNTRIES.get(country.upper())
    if known is None:
        return None
    default_language, currency = known
    return {
        "country": country.upper(),
        "language": (language or default_language).lower()[:5],
        "currency": currency,
    }


async def fetch(
    url: str, *, settings: Settings, client: httpx.AsyncClient | None = None
) -> httpx.Response | None:
    """One GET, following redirects, or None if the address did not answer.

    The single place either detector reaches the network, so a test replaces
    one function and neither can quietly take a different route.
    """
    owned = client is None
    client = client or httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(settings.connector_timeout_s),
        headers={"User-Agent": settings.connector_user_agent},
    )
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        log.info("autofill.unreachable", url=url, error=str(exc))
        return None
    finally:
        if owned:
            await client.aclose()
    return response if response.status_code < 400 else None


async def detect_site_url(
    domain: str, *, settings: Settings, client: httpx.AsyncClient | None = None
) -> Finding:
    """Where the domain actually lands, after every redirect it wants to make."""
    for scheme in ("https", "http"):
        response = await fetch(f"{scheme}://{domain}", settings=settings, client=client)
        if response is None:
            continue
        final = str(response.url)
        # Keep the path: a site that redirects to /us/ is telling you where its
        # content lives, and crawling the bare host would find a language picker.
        final = final.split("?")[0].split("#")[0].rstrip("/") or final
        moved = urlparse(final).netloc != domain or urlparse(final).path not in ("", "/")
        return Finding(
            field="site_url",
            value=final,
            source=(f"{domain} redirects to {final}" if moved else f"{domain} answered directly"),
            found=True,
        )
    return Finding(
        field="site_url",
        value="",
        source=f"{domain} did not answer over https or http — type the address to crawl",
        found=False,
    )


def _hreflang_countries(html: str) -> list[tuple[str, str | None]]:
    """The countries a page declares, best first.

    Document order is not a ranking — a site listing thirty locales lists them
    alphabetically or by region, neither of which says where it sells. What
    does say something is whether a locale has a page of its own: `en-GB` →
    `/uk/` is a market somebody built for, while twenty locales all pointing at
    `/eu/` are one page with twenty labels on it. So locales with a URL nobody
    else shares come first, and the rest keep the order they were written in.
    """
    tree = HTMLParser(html)
    found: list[tuple[str, str | None, str]] = []
    seen: set[str] = set()
    for node in tree.css("link[rel=alternate][hreflang]"):
        value = (node.attributes.get("hreflang") or "").strip()
        if value.lower() in ("x-default", ""):
            continue
        match = HREFLANG.match(value)
        if match is None:
            continue
        language, country = match.group(1), match.group(2)
        if not country:
            # `hreflang="nb"` names a language, not a country, and a market is a
            # country. Reading Norway out of Norwegian is how NO and SE become
            # one market.
            continue
        country = country.upper()
        if country in seen:
            continue
        seen.add(country)
        found.append((country, language, (node.attributes.get("href") or "").strip()))

    shared = Counter(href for _, _, href in found if href)
    return [
        (country, language)
        for country, language, href in sorted(
            found, key=lambda row: (shared[row[2]] > 1, found.index(row))
        )
    ]


async def _crm_countries(db: AsyncSession, project: Project) -> tuple[Counter[str], int]:
    """How often each country appears in this project's CRM rows, and how many did not resolve."""
    rows = (
        await db.execute(
            sa.select(Evidence.payload).where(
                Evidence.project_id == project.id, Evidence.kind.in_(CRM_KINDS)
            )
        )
    ).scalars()
    counts: Counter[str] = Counter()
    unresolved = 0
    for payload in rows:
        raw = (payload or {}).get("country")
        if not raw:
            continue
        country = normalise_country(raw)
        if country is None:
            unresolved += 1
            continue
        counts[country] += 1
    return counts, unresolved


def compose_markets(
    crm: Counter[str], published: list[tuple[str, str | None]]
) -> tuple[list[dict[str, str]], list[str], int]:
    """Merge the two sources into a proposal: (markets, unrecognised, dropped).

    The CRM goes first and the website second, and the order is the argument:
    one is where revenue actually came from, the other is where somebody
    decided to publish a translation.
    """
    ordered: list[tuple[str, str | None]] = [(country, None) for country, _ in crm.most_common()]
    seen = {country for country, _ in ordered}
    ordered.extend((country, language) for country, language in published if country not in seen)

    markets: list[dict[str, str]] = []
    unknown: list[str] = []
    for country, language in ordered:
        if len(markets) >= MAX_MARKETS:
            break
        market = market_for(country, language)
        if market is None:
            unknown.append(country)
            continue
        markets.append(market)
    dropped = max(0, len(ordered) - len(markets) - len(unknown))
    return markets, unknown, dropped


async def detect_markets(
    db: AsyncSession,
    project: Project,
    *,
    settings: Settings,
    site_url: str = "",
    client: httpx.AsyncClient | None = None,
) -> Finding:
    """The countries this business sells into, read rather than guessed.

    The CRM export goes first and the website second, and the order is the
    argument: one is where revenue actually came from, the other is where
    someone decided to publish a translation.
    """
    counts, unresolved = await _crm_countries(db, project)
    reasons: list[str] = []
    if counts:
        top = ", ".join(country for country, _ in counts.most_common(MAX_MARKETS))
        reasons.append(f"your CRM export names {len(counts)} countries — {top} lead it")
    if unresolved:
        reasons.append(f"{unresolved} CRM rows had a country nothing could read")

    published: list[tuple[str, str | None]] = []
    target = site_url or (f"https://{project.domain}" if project.domain else "")
    if target:
        response = await fetch(target, settings=settings, client=client)
        if response is not None:
            published = _hreflang_countries(response.text)
            fresh = [pair for pair in published if pair[0] not in counts]
            if published:
                reasons.append(
                    f"the site publishes {len(published)} language links"
                    + (f", adding {', '.join(c for c, _ in fresh[:MAX_MARKETS])}" if fresh else "")
                )

    markets, unknown, dropped = compose_markets(counts, published)
    if dropped:
        # Never silently. A capped list that reads as a complete one is the
        # quiet version of getting this wrong.
        reasons.append(f"{dropped} more were left out — the first {MAX_MARKETS} are the proposal")
    if unknown:
        reasons.append(f"no currency on file for {', '.join(sorted(set(unknown)))}")

    if not markets:
        return Finding(
            field="markets",
            value=[],
            source=(
                "; ".join(reasons)
                or "nothing here names a country yet — upload a CRM export, or add one by hand"
            ),
            found=False,
        )
    return Finding(field="markets", value=markets, source="; ".join(reasons), found=True)
