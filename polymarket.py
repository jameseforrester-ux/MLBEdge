"""Polymarket integration: daily highest-temperature markets.

For each (city, date) we look up the Polymarket *event* on the public Gamma
API, then pull all child markets (each market = a temperature bucket like
"22°F", "≥30°C", "≤20°F or below"). We extract:

- the YES probability (the last trade price for the YES token)
- the bucket type: 'exact' / 'gte' / 'lte'
- the bucket temperature value, in the unit the market is denominated in

We then expose:

- get_market_for_city(city, date)            -> Optional[CityMarket]
- top_n_by_yes(market, n)                    -> sorted list
- match_for_prediction(market, predicted_°)  -> the bucket our model agrees with
- hedges_around(market, predicted_°, k)      -> a hedge band around our pick
- buy_url(market)                            -> deep link into Polymarket

Polymarket only runs daily highest-temperature markets for ~10 cities; for
everything else we silently return None and the bot hides the section.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"
EVENT_BY_SLUG = GAMMA_BASE + "/events/slug/{slug}"
SITE_BASE = "https://polymarket.com"

# Cities Polymarket runs daily highest-temp markets for. Keys are the canonical
# city name we use for lookups; values are (slug-fragment, unit, airport-codes).
# Unit: 'C' for Celsius markets, 'F' for Fahrenheit markets.
# Airport codes are ICAO codes that map back to this city.
SUPPORTED_CITIES: Dict[str, Tuple[str, str, List[str]]] = {
    # NOTE: city_key is the internal id; first tuple field is the URL slug
    # Polymarket actually uses, which differs for NYC ("nyc" not "new-york").
    "nyc":          ("nyc",          "F", ["KJFK", "KLGA", "KEWR"]),
    "los-angeles":  ("los-angeles",  "F", ["KLAX", "KBUR", "KLGB", "KSNA"]),
    "chicago":      ("chicago",      "F", ["KORD", "KMDW"]),
    "miami":        ("miami",        "F", ["KMIA", "KFLL"]),
    "philadelphia": ("philadelphia", "F", ["KPHL"]),
    "austin":       ("austin",       "F", ["KAUS"]),
    "denver":       ("denver",       "F", ["KDEN", "KAPA"]),
    "houston":      ("houston",      "F", ["KIAH", "KHOU"]),
    "atlanta":      ("atlanta",      "F", ["KATL"]),
    "dallas":       ("dallas",       "F", ["KDFW", "KDAL"]),
    "seattle":      ("seattle",      "F", ["KSEA", "KBFI"]),
    "san-francisco":("san-francisco","F", ["KSFO", "KOAK", "KSJC"]),
    "toronto":      ("toronto",      "C", ["CYYZ", "CYTZ"]),
    "london":       ("london",       "C", ["EGLL", "EGKK", "EGLC", "EGSS", "EGGW"]),
    "paris":        ("paris",        "C", ["LFPG", "LFPO", "LFPB"]),
    "tokyo":        ("tokyo",        "C", ["RJTT", "RJAA"]),
}

# Display labels for cities whose key isn't a clean title-case
CITY_DISPLAY = {
    "nyc": "NYC",
    "los-angeles": "Los Angeles",
    "san-francisco": "San Francisco",
}

# Build reverse lookup: ICAO -> (city-key, unit). One airport always maps to
# at most one Polymarket city (we don't dilute Newark across two markets).
ICAO_TO_CITY: Dict[str, Tuple[str, str]] = {}
for city_key, (_, unit, codes) in SUPPORTED_CITIES.items():
    for icao in codes:
        ICAO_TO_CITY[icao] = (city_key, unit)


@dataclass
class TempBucket:
    """A single temperature bucket inside a daily market event.

    Buckets can be:
      - 'range' : a closed interval [value, high_value] (e.g. 62-63°F)
      - 'exact' : value == high_value          (e.g. 22°C)
      - 'gte'   : open-ended  ≥ value          (e.g. ≥30°C)
      - 'lte'   : open-ended  ≤ value          (e.g. ≤6°C)
    """
    label: str
    value: int                # for ranges, the LOW end (also kind-tag value for gte/lte)
    kind: str                 # 'exact' | 'range' | 'lte' | 'gte'
    yes_prob: float           # 0..1
    market_slug: str
    high_value: Optional[int] = None  # only set for 'range'; defaults to == value
    yes_token_id: Optional[str] = None

    @property
    def hi(self) -> int:
        return self.high_value if self.high_value is not None else self.value

    @property
    def midpoint(self) -> float:
        return (self.value + self.hi) / 2.0

    def matches(self, predicted_int_temp: int) -> bool:
        """Does this bucket cover the model's predicted (integer) temperature?"""
        if self.kind == "exact":
            return predicted_int_temp == self.value
        if self.kind == "range":
            return self.value <= predicted_int_temp <= self.hi
        if self.kind == "gte":
            return predicted_int_temp >= self.value
        if self.kind == "lte":
            return predicted_int_temp <= self.value
        return False

    @property
    def trade_url(self) -> str:
        return f"{SITE_BASE}/market/{self.market_slug}"


@dataclass
class CityMarket:
    """A whole daily event for one city — multiple TempBuckets."""
    city_key: str             # e.g. 'toronto'
    city_display: str         # 'Toronto'
    unit: str                 # 'C' or 'F'
    event_slug: str
    event_title: str
    target_date: date
    buckets: List[TempBucket]

    @property
    def event_url(self) -> str:
        return f"{SITE_BASE}/event/{self.event_slug}"


# ─────────────────────────── city lookup ──────────────────────────────────
def city_for_airport(icao: str) -> Optional[Tuple[str, str]]:
    """Returns (city_key, unit) if this ICAO maps to a covered Polymarket city."""
    return ICAO_TO_CITY.get((icao or "").upper())


# ─────────────────────────── slug enumeration ─────────────────────────────
# Polymarket uses several historical slug formats for daily high-temp events.
# We try the new format first (numeric date) then the old long-form names,
# both with and without a -fahrenheit/-celsius suffix.
_MONTH_NAMES = [m.lower() for m in calendar.month_name[1:]]


def _candidate_event_slugs(city_slug: str, d: date) -> List[str]:
    month_name = _MONTH_NAMES[d.month - 1]
    yyyy_mm_dd = d.strftime("%Y-%m-%d")
    yymmdd = d.strftime("%y%m%d")
    return [
        # Newer numeric formats observed in 2025–2026
        f"highest-temperature-in-{city_slug}-on-{yyyy_mm_dd}",
        f"highest-temperature-in-{city_slug}-{yyyy_mm_dd}",
        f"highest-temperature-{city_slug}-{yymmdd}",
        # Older worded formats
        f"highest-temperature-in-{city_slug}-on-{month_name}-{d.day}-{d.year}",
        f"highest-temperature-in-{city_slug}-{month_name}-{d.day}-{d.year}",
        # Some have a unit suffix
        f"highest-temperature-in-{city_slug}-on-{month_name}-{d.day}",
        f"highest-temperature-{city_slug}-{month_name}-{d.day}-{d.year}",
    ]


# ─────────────────────────── bucket parsing ───────────────────────────────
# Examples seen in real market questions:
#   "Will the highest temperature in NYC be between 62-63°F on April 6?"
#   "Will the highest temperature in NYC be 78-79°F on April 1?"
#   "Will the highest temperature in NYC be 75°F or above on April 29?"
#   "Will the highest temperature in NYC be 37°F or below on April 8?"
#   "Will the highest temperature in Toronto be 22°C on April 29?"
#
# Polymarket bins NYC etc. into 2°F ranges. Toronto is binned in 1°C steps.

# Range first: "62-63°F" or "78-79°F" or "62 - 63 F"
_RANGE_RE = re.compile(
    r"(-?\d{1,3})\s*-\s*(-?\d{1,3})\s*[°º]?\s*([CF])\b",
    re.IGNORECASE,
)
# Open-ended: "75°F or above", "6°C or below"
_OPEN_RE = re.compile(
    r"(-?\d{1,3})\s*[°º]?\s*([CF])\b\s*"
    r"or\s+(?P<qual>above|higher|more|below|lower|less)",
    re.IGNORECASE,
)
# Single exact: "22°C", "80°F"  (used as a fallback)
_SINGLE_RE = re.compile(
    r"(-?\d{1,3})\s*[°º]?\s*([CF])\b",
    re.IGNORECASE,
)


def _parse_bucket(question: str) -> Optional[Tuple[int, int, str, str]]:
    """Returns (low_value, high_value, kind, unit) or None.

    For ranges:    low<high, kind='range'   (e.g. 62, 63, 'range', 'F')
    For exact:     low=high,  kind='exact'  (e.g. 22, 22, 'exact', 'C')
    For ≥ tail:    high=low,  kind='gte'    (e.g. 30, 30, 'gte', 'C')
    For ≤ tail:    high=low,  kind='lte'    (e.g.  6,  6, 'lte', 'C')
    """
    if not question:
        return None

    # Try open-ended first to win over the trailing single-temp regex
    m = _OPEN_RE.search(question)
    if m:
        try:
            v = int(m.group(1))
        except ValueError:
            return None
        unit = m.group(2).upper()
        qual = (m.group("qual") or "").lower()
        kind = "gte" if qual in ("above", "higher", "more") else "lte"
        return v, v, kind, unit

    # Try range next
    m = _RANGE_RE.search(question)
    if m:
        try:
            lo = int(m.group(1))
            hi = int(m.group(2))
        except ValueError:
            return None
        if lo > hi:
            lo, hi = hi, lo
        unit = m.group(3).upper()
        return lo, hi, "range", unit

    # Fallback: single exact temperature
    m = _SINGLE_RE.search(question)
    if m:
        try:
            v = int(m.group(1))
        except ValueError:
            return None
        unit = m.group(2).upper()
        return v, v, "exact", unit

    return None


def _parse_outcome_prices(raw: object) -> List[float]:
    """outcomePrices comes back as a JSON-encoded string like '["0.94","0.06"]'."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            import json
            items = json.loads(s)
        except Exception:
            return []
    else:
        return []
    out = []
    for it in items:
        try:
            out.append(float(it))
        except (TypeError, ValueError):
            continue
    return out


def _parse_yes_token_id(market: dict) -> Optional[str]:
    """The first id in clobTokenIds[] is the YES token."""
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            import json
            ids = json.loads(raw)
        except Exception:
            return None
    elif isinstance(raw, list):
        ids = raw
    else:
        return None
    return ids[0] if ids else None


# ─────────────────────────── fetcher ──────────────────────────────────────
async def _fetch_event(client: httpx.AsyncClient, slug: str) -> Optional[dict]:
    try:
        r = await client.get(EVENT_BY_SLUG.format(slug=slug))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) and data.get("markets") else None
    except httpx.HTTPError:
        return None


async def get_market_for_city(
    city_key: str, target_date: date, expected_unit: str
) -> Optional[CityMarket]:
    """Look up the daily Polymarket event for this city+date and parse buckets."""
    cfg = SUPPORTED_CITIES.get(city_key)
    if not cfg:
        return None
    city_slug, unit, _ = cfg
    if unit != expected_unit:
        # sanity check — caller should have used the unit from the city config
        unit = expected_unit

    slugs = _candidate_event_slugs(city_slug, target_date)
    event = None
    matched_slug = None
    async with httpx.AsyncClient(timeout=10) as client:
        for s in slugs:
            event = await _fetch_event(client, s)
            if event:
                matched_slug = s
                break
    if not event:
        return None

    buckets: List[TempBucket] = []
    for m in event.get("markets") or []:
        # Skip closed/inactive markets
        if m.get("closed") is True:
            continue
        if m.get("active") is False:
            continue

        question = m.get("question") or ""
        parsed = _parse_bucket(question)
        if not parsed:
            continue
        lo, hi, kind, m_unit = parsed
        if m_unit != unit:
            # Different unit from city default — defer to the market's actual unit
            unit = m_unit

        prices = _parse_outcome_prices(m.get("outcomePrices"))
        if not prices:
            # Fallback to bestBid as a price approximation
            try:
                yes_p = float(m.get("bestBid") or 0)
            except (TypeError, ValueError):
                continue
        else:
            yes_p = prices[0]  # YES is always index 0 on binary markets

        if not (0 <= yes_p <= 1):
            continue

        # Build a friendly label
        if kind == "exact":
            label = f"{lo}°{unit}"
        elif kind == "range":
            label = f"{lo}–{hi}°{unit}"
        elif kind == "gte":
            label = f"≥{lo}°{unit}"
        else:  # lte
            label = f"≤{lo}°{unit}"

        buckets.append(
            TempBucket(
                label=label,
                value=lo,
                high_value=hi if kind == "range" else None,
                kind=kind,
                yes_prob=yes_p,
                market_slug=m.get("slug") or matched_slug,
                yes_token_id=_parse_yes_token_id(m),
            )
        )

    if not buckets:
        return None

    return CityMarket(
        city_key=city_key,
        city_display=CITY_DISPLAY.get(city_key, city_key.replace("-", " ").title()),
        unit=unit,
        event_slug=matched_slug,
        event_title=event.get("title") or "",
        target_date=target_date,
        buckets=buckets,
    )


# ─────────────────────────── selectors ────────────────────────────────────
def top_n_by_yes(market: CityMarket, n: int = 3) -> List[TempBucket]:
    return sorted(market.buckets, key=lambda b: -b.yes_prob)[:n]


def match_for_prediction(market: CityMarket, predicted: int) -> Optional[TempBucket]:
    """The bucket our model's prediction lands in.

    Priority: exact equal > range containing > tail bucket containing.
    """
    # 1. exact bucket equal to prediction
    for b in market.buckets:
        if b.kind == "exact" and b.value == predicted:
            return b
    # 2. range bucket containing prediction
    for b in market.buckets:
        if b.kind == "range" and b.value <= predicted <= b.hi:
            return b
    # 3. open-ended tail bucket containing prediction
    for b in market.buckets:
        if b.kind in ("gte", "lte") and b.matches(predicted):
            return b
    return None


def hedges_around(
    market: CityMarket, predicted: int, k: int = 1
) -> List[TempBucket]:
    """The model's matched bucket plus k buckets on each side.

    Works for both exact-bucket markets (Toronto) and range-bucket markets
    (NYC/LA/etc), by sorting all closed buckets along a single axis using
    their midpoint, then fanning out k slots above and below the closest one.
    Open-ended tail buckets that contain the prediction are appended too.
    """
    closed = sorted(
        [b for b in market.buckets if b.kind in ("exact", "range")],
        key=lambda b: b.midpoint,
    )
    if not closed:
        return []

    # Find the closed bucket closest to the prediction
    closest_idx = min(
        range(len(closed)),
        key=lambda i: abs(closed[i].midpoint - predicted),
    )
    lo = max(0, closest_idx - k)
    hi = min(len(closed), closest_idx + k + 1)
    band = closed[lo:hi]

    # Add open-ended tail buckets that cover the prediction (but aren't dupes)
    for b in market.buckets:
        if b.kind in ("gte", "lte") and b.matches(predicted):
            if not any(x.market_slug == b.market_slug for x in band):
                band.append(b)
    return band
