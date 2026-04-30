"""Async client for Polymarket's Gamma (metadata) and CLOB (order book) APIs.

Polymarket exposes two relevant public REST services:

* Gamma API — indexed market/event metadata, tags, sports info.
* CLOB API  — live order books, last trade prices, midpoints per token id.

This module is intentionally small: it returns raw dicts and lets the parser
turn them into domain objects. That keeps the API surface easy to mock in tests.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional

import httpx

logger = logging.getLogger(__name__)


# Polymarket's "MLB" series/tag slug. The Gamma API exposes a /sports endpoint
# that lists tags per sport; "mlb" is the canonical slug used both as a tag
# slug and within event slugs.
MLB_TAG_SLUG = "mlb"
MLB_KEYWORDS = (
    "mlb",
    "yankees", "red sox", "blue jays", "orioles", "rays",
    "guardians", "twins", "white sox", "tigers", "royals",
    "astros", "rangers", "angels", "mariners", "athletics",
    "braves", "phillies", "mets", "marlins", "nationals",
    "brewers", "cubs", "cardinals", "reds", "pirates",
    "dodgers", "padres", "giants", "diamondbacks", "rockies",
)


class PolymarketClient:
    """Thin async wrapper around the two Polymarket public REST APIs."""

    def __init__(
        self,
        gamma_url: str = "https://gamma-api.polymarket.com",
        clob_url: str = "https://clob.polymarket.com",
        timeout: float = 15.0,
    ) -> None:
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        # A shared client preserves the HTTP/2 connection across calls.
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "polymarket-mlb-bot/1.0"},
            http2=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----- Gamma --------------------------------------------------------- #

    async def get_events(
        self,
        *,
        tag_slug: Optional[str] = None,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Fetch events (a Polymarket "event" groups markets for one game)."""
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": "startDate",
            "ascending": "true",
        }
        if tag_slug:
            params["tag_slug"] = tag_slug

        url = f"{self.gamma_url}/events"
        try:
            r = await self._http.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            logger.warning("Gamma /events request failed: %s", e)
            return []
        return data if isinstance(data, list) else []

    async def get_event_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch a single event by its slug. Tries the dedicated endpoint first
        and falls back to a slug-filtered list query."""
        # Try direct lookup
        try:
            r = await self._http.get(f"{self.gamma_url}/events/slug/{slug}")
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError:
            pass

        # Fallback: filter list
        try:
            r = await self._http.get(f"{self.gamma_url}/events", params={"slug": slug})
            r.raise_for_status()
            arr = r.json()
            if isinstance(arr, list) and arr:
                return arr[0]
        except httpx.HTTPError as e:
            logger.warning("Gamma slug fallback failed for %s: %s", slug, e)
        return None

    async def get_mlb_events(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Convenience: fetch active MLB events.

        Strategy:
          1. Try the official MLB tag slug.
          2. If that yields nothing (tag id changes), fall back to a broader
             scan and filter by keyword in the event title or slug.
        """
        evts = await self.get_events(tag_slug=MLB_TAG_SLUG, limit=limit)
        if evts:
            return evts

        logger.info("No MLB-tagged events; falling back to keyword scan")
        broad = await self.get_events(limit=limit)
        out: List[Dict[str, Any]] = []
        for e in broad:
            blob = " ".join(
                str(e.get(k, "")) for k in ("title", "slug", "description")
            ).lower()
            if any(kw in blob for kw in MLB_KEYWORDS):
                out.append(e)
        return out

    # ----- CLOB ---------------------------------------------------------- #

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """Mid-market price for a single token id (0..1)."""
        try:
            r = await self._http.get(
                f"{self.clob_url}/midpoint", params={"token_id": token_id}
            )
            r.raise_for_status()
            data = r.json()
            mid = data.get("mid")
            return float(mid) if mid is not None else None
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("CLOB midpoint failed for %s: %s", token_id, e)
            return None

    async def get_book(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Full order book for a token. Useful for computing bid/ask spread."""
        try:
            r = await self._http.get(
                f"{self.clob_url}/book", params={"token_id": token_id}
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            logger.debug("CLOB book failed for %s: %s", token_id, e)
            return None

    async def get_midpoints(self, token_ids: Iterable[str]) -> Dict[str, float]:
        """Batch midpoint fetch. Falls back to parallel single calls if the
        batch endpoint is unavailable."""
        ids = [t for t in token_ids if t]
        if not ids:
            return {}

        # Try batch endpoint first.
        try:
            r = await self._http.post(
                f"{self.clob_url}/midpoints",
                json={"params": [{"token_id": t} for t in ids]},
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    out: Dict[str, float] = {}
                    for k, v in data.items():
                        try:
                            out[k] = float(v)
                        except (TypeError, ValueError):
                            continue
                    if out:
                        return out
        except httpx.HTTPError:
            pass

        # Fallback: parallel singles
        results = await asyncio.gather(*(self.get_midpoint(t) for t in ids))
        return {tid: px for tid, px in zip(ids, results) if px is not None}
