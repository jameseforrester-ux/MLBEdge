"""Parse raw Polymarket events into typed MLB game/market objects.

A Polymarket "event" for a baseball game (e.g. "Yankees vs Red Sox - 2026-04-30")
typically contains 3 distinct markets:

  * Moneyline   — "Will the Yankees win?"
  * Run line    — "Will the Yankees cover -1.5?"
  * Total runs  — "Will total runs be over 8.5?"

Each market is a binary YES/NO with two CLOB token ids. The price of the YES
token, in USDC between $0 and $1, is the implied probability of YES resolving.

This module is purely a data layer — no network calls, no analysis. It just
takes the JSON Polymarket returns and gives us nicely typed records to work
with elsewhere.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class MarketKind(str, Enum):
    MONEYLINE = "moneyline"
    RUNLINE = "runline"      # spread (typically ±1.5)
    TOTAL = "total"          # over/under
    OTHER = "other"


@dataclass
class Outcome:
    """One side of a binary market (YES or NO)."""
    label: str                # e.g. "Yankees", "Over 8.5", "Yes"
    token_id: str             # CLOB token id used for live prices
    price: float              # implied probability, 0..1


@dataclass
class Market:
    market_id: str
    question: str
    slug: str
    kind: MarketKind
    outcomes: List[Outcome]   # exactly 2 for binary markets
    liquidity: float          # USDC committed to the order book
    volume: float             # cumulative USDC traded
    line: Optional[float] = None   # numeric line for spreads/totals
    end_date: Optional[datetime] = None

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2


@dataclass
class Game:
    """One MLB game — typically the union of moneyline + runline + totals."""
    event_id: str
    slug: str
    title: str                  # raw event title from Polymarket
    away_team: Optional[str]
    home_team: Optional[str]
    start_time: Optional[datetime]
    markets: List[Market] = field(default_factory=list)

    @property
    def matchup(self) -> str:
        if self.away_team and self.home_team:
            return f"{self.away_team} @ {self.home_team}"
        return self.title

    def market(self, kind: MarketKind) -> Optional[Market]:
        for m in self.markets:
            if m.kind == kind:
                return m
        return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Use \b word boundaries so "cover" doesn't match the "over" total check.
_TOTAL_LINE_RE = re.compile(r"\b(?:over|under)\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_TOTAL_KW_RE = re.compile(r"\b(?:over|under|total\s+runs)\b", re.IGNORECASE)
_RUNLINE_KW_RE = re.compile(r"\b(?:run\s*line|runline|cover|spread)\b", re.IGNORECASE)
_RUNLINE_LINE_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*runs?", re.IGNORECASE)
_MONEYLINE_KW_RE = re.compile(r"\b(?:win|winner|to\s+win|moneyline)\b", re.IGNORECASE)


def _classify(question: str) -> Tuple[MarketKind, Optional[float]]:
    """Best-effort classification of a market by its question text.

    Order matters: run line must be checked BEFORE totals because "cover"
    contains the substring "over".
    """
    q = question.lower()

    if _RUNLINE_KW_RE.search(q):
        m = _RUNLINE_LINE_RE.search(q)
        line = float(m.group(1)) if m else None
        return MarketKind.RUNLINE, line

    if _TOTAL_KW_RE.search(q):
        m = _TOTAL_LINE_RE.search(q)
        line = float(m.group(1)) if m else None
        return MarketKind.TOTAL, line

    if _MONEYLINE_KW_RE.search(q):
        return MarketKind.MONEYLINE, None

    return MarketKind.OTHER, None


_VS_RE = re.compile(r"(.+?)\s+(?:vs\.?|@)\s+(.+)", re.IGNORECASE)
# Anything after a " - " or " — " followed by a digit/month is event metadata,
# not part of the team name.
_TRAILING_META_RE = re.compile(
    r"\s*[-—–]\s*"
    r"(?:\d|january|february|march|april|may|june|july|"
    r"august|september|october|november|december|jan|feb|mar|apr|jun|jul|"
    r"aug|sep|sept|oct|nov|dec).*$",
    re.IGNORECASE,
)


def _strip_trailing_meta(name: str) -> str:
    name = _TRAILING_META_RE.sub("", name)
    # Also strip any trailing time fragments like " 7:05pm"
    name = re.split(r"\s+\d{1,2}[:/]", name)[0]
    return name.strip()


def _teams_from_title(title: str) -> Tuple[Optional[str], Optional[str]]:
    """Pull (away, home) team names out of an event title like
    'Yankees vs Red Sox - April 30'. Polymarket isn't perfectly consistent so
    this is best-effort."""
    if not title:
        return None, None
    m = _VS_RE.search(title)
    if not m:
        return None, None
    a = _strip_trailing_meta(m.group(1).strip())
    b = _strip_trailing_meta(m.group(2).strip())
    return a or None, b or None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # Polymarket returns ISO 8601 with trailing Z.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _decode_list(value: Any) -> List[Any]:
    """Polymarket returns several list-like fields as JSON-encoded strings.
    e.g. outcomes='["Yes","No"]' or clobTokenIds='["12345","67890"]'."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def parse_market(raw: Dict[str, Any]) -> Optional[Market]:
    """Convert a Gamma `Market` dict into our Market dataclass.
    Returns None if the market is non-binary or missing token ids."""
    question = (raw.get("question") or raw.get("title") or "").strip()
    slug = raw.get("slug") or ""
    market_id = str(raw.get("id") or raw.get("conditionId") or slug)

    outcomes_raw = _decode_list(raw.get("outcomes"))
    prices_raw = _decode_list(raw.get("outcomePrices"))
    token_ids = _decode_list(raw.get("clobTokenIds"))

    if len(outcomes_raw) != 2 or len(token_ids) != 2:
        return None

    # Prices may be missing on freshly-created markets; default to 0.5.
    prices = [_to_float(p, 0.5) for p in prices_raw] if prices_raw else [0.5, 0.5]
    if len(prices) != 2:
        prices = [0.5, 0.5]

    outcomes = [
        Outcome(label=str(outcomes_raw[i]), token_id=str(token_ids[i]), price=prices[i])
        for i in range(2)
    ]

    kind, line = _classify(question)

    return Market(
        market_id=market_id,
        question=question,
        slug=slug,
        kind=kind,
        outcomes=outcomes,
        liquidity=_to_float(raw.get("liquidity") or raw.get("liquidityNum")),
        volume=_to_float(raw.get("volume") or raw.get("volumeNum")),
        line=line,
        end_date=_parse_dt(raw.get("endDate") or raw.get("end_date")),
    )


def parse_event(raw: Dict[str, Any]) -> Optional[Game]:
    """Convert a Gamma `Event` dict into a Game with its child markets."""
    title = raw.get("title") or raw.get("question") or ""
    slug = raw.get("slug") or ""
    event_id = str(raw.get("id") or slug)
    away, home = _teams_from_title(title)
    start = _parse_dt(raw.get("startDate") or raw.get("start_date") or raw.get("creationDate"))

    markets: List[Market] = []
    for m_raw in raw.get("markets") or []:
        m = parse_market(m_raw)
        if m is not None:
            markets.append(m)

    if not markets:
        return None

    return Game(
        event_id=event_id,
        slug=slug,
        title=title,
        away_team=away,
        home_team=home,
        start_time=start,
        markets=markets,
    )


def parse_events(raw_events: List[Dict[str, Any]]) -> List[Game]:
    out: List[Game] = []
    for raw in raw_events:
        try:
            g = parse_event(raw)
            if g is not None:
                out.append(g)
        except Exception:
            logger.exception("Failed to parse event %s", raw.get("slug"))
    return out


def apply_live_prices(game: Game, midpoints: Dict[str, float]) -> None:
    """Overwrite each outcome's `price` with the latest CLOB midpoint when
    available. Mutates the game in place."""
    for m in game.markets:
        for o in m.outcomes:
            mid = midpoints.get(o.token_id)
            if mid is not None and 0.0 <= mid <= 1.0:
                o.price = mid


def upcoming_only(games: List[Game], *, now: Optional[datetime] = None) -> List[Game]:
    """Filter to games whose start time is in the future (or unknown)."""
    now = now or datetime.now(tz=timezone.utc)
    out: List[Game] = []
    for g in games:
        if g.start_time is None or g.start_time >= now:
            out.append(g)
    return out
