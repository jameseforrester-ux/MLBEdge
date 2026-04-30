"""External data enrichment.

We use two free, no-key public APIs to build a fair-probability prior we can
compare against Polymarket's market-implied probability:

* MLB Stats API   (statsapi.mlb.com) — schedule, team records, probable pitchers,
                                       team season-to-date run scoring/allowing.
* Open-Meteo      (api.open-meteo.com) — wind/temperature at the ballpark for
                                          totals adjustments.

Both are HTTP, no auth, no rate limits worth caring about for a personal bot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

# Approximate venue coordinates. Used only for weather lookups so coarse is fine.
# Source: public stadium coordinates (Wikipedia / MLB.com).
VENUE_COORDS: Dict[str, Tuple[float, float]] = {
    "Yankee Stadium":          (40.8296, -73.9262),
    "Fenway Park":             (42.3467, -71.0972),
    "Rogers Centre":           (43.6414, -79.3894),
    "Oriole Park at Camden Yards": (39.2839, -76.6217),
    "Tropicana Field":         (27.7682, -82.6534),
    "Progressive Field":       (41.4962, -81.6852),
    "Target Field":            (44.9817, -93.2776),
    "Guaranteed Rate Field":   (41.8300, -87.6338),
    "Comerica Park":           (42.3390, -83.0485),
    "Kauffman Stadium":        (39.0517, -94.4803),
    "Minute Maid Park":        (29.7572, -95.3555),
    "Globe Life Field":        (32.7473, -97.0817),
    "Angel Stadium":           (33.8003, -117.8827),
    "T-Mobile Park":           (47.5914, -122.3325),
    "Oakland Coliseum":        (37.7516, -122.2008),
    "Truist Park":             (33.8908, -84.4678),
    "Citizens Bank Park":      (39.9061, -75.1665),
    "Citi Field":              (40.7571, -73.8458),
    "loanDepot park":          (25.7781, -80.2197),
    "Nationals Park":          (38.8730, -77.0074),
    "American Family Field":   (43.0280, -87.9712),
    "Wrigley Field":           (41.9484, -87.6553),
    "Busch Stadium":           (38.6226, -90.1928),
    "Great American Ball Park": (39.0975, -84.5069),
    "PNC Park":                (40.4469, -80.0058),
    "Dodger Stadium":          (34.0739, -118.2400),
    "Petco Park":              (32.7073, -117.1566),
    "Oracle Park":             (37.7786, -122.3893),
    "Chase Field":             (33.4453, -112.0667),
    "Coors Field":             (39.7559, -104.9942),
}


@dataclass
class TeamForm:
    name: str
    wins: int = 0
    losses: int = 0
    runs_scored: int = 0
    runs_allowed: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def win_pct(self) -> float:
        return self.wins / self.games if self.games else 0.5

    @property
    def pythag_win_pct(self) -> float:
        """Pythagorean expectation, exponent 1.83 (Bill James, baseball-tuned)."""
        rs, ra = self.runs_scored, self.runs_allowed
        if rs == 0 and ra == 0:
            return 0.5
        try:
            return rs**1.83 / (rs**1.83 + ra**1.83)
        except ZeroDivisionError:
            return 0.5

    @property
    def runs_per_game(self) -> float:
        return self.runs_scored / self.games if self.games else 4.5


@dataclass
class GameContext:
    away_form: Optional[TeamForm] = None
    home_form: Optional[TeamForm] = None
    away_pitcher: Optional[str] = None
    home_pitcher: Optional[str] = None
    away_pitcher_era: Optional[float] = None
    home_pitcher_era: Optional[float] = None
    venue: Optional[str] = None
    wind_mph: Optional[float] = None
    wind_dir_deg: Optional[float] = None       # 0=N, 90=E, 180=S, 270=W
    temperature_c: Optional[float] = None
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# MLB Stats API
# --------------------------------------------------------------------------- #

async def fetch_schedule(client: httpx.AsyncClient, on: date) -> List[Dict]:
    """Return MLB games scheduled on `on` with probable pitchers + venue."""
    url = f"{MLB_STATS_BASE}/schedule"
    params = {
        "sportId": 1,
        "date": on.isoformat(),
        "hydrate": "probablePitcher,venue,team",
    }
    try:
        r = await client.get(url, params=params, timeout=10.0)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        logger.warning("MLB schedule fetch failed: %s", e)
        return []
    out: List[Dict] = []
    for d in data.get("dates", []):
        out.extend(d.get("games", []))
    return out


async def fetch_team_records(client: httpx.AsyncClient) -> Dict[str, TeamForm]:
    """Return season-to-date win/loss + run totals keyed by team display name."""
    try:
        r = await client.get(
            f"{MLB_STATS_BASE}/standings",
            params={"leagueId": "103,104", "standingsTypes": "regularSeason"},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        logger.warning("MLB standings fetch failed: %s", e)
        return {}

    out: Dict[str, TeamForm] = {}
    for record in data.get("records", []):
        for tr in record.get("teamRecords", []):
            team = tr.get("team", {})
            name = team.get("name") or team.get("teamName")
            if not name:
                continue
            out[name] = TeamForm(
                name=name,
                wins=int(tr.get("wins", 0)),
                losses=int(tr.get("losses", 0)),
                runs_scored=int(tr.get("runsScored", 0) or 0),
                runs_allowed=int(tr.get("runsAllowed", 0) or 0),
            )
            # Also key by short team name to make matching looser (e.g. "Yankees")
            short = team.get("teamName")
            if short and short != name:
                out[short] = out[name]
    return out


def _match_team(needle: Optional[str], records: Dict[str, TeamForm]) -> Optional[TeamForm]:
    if not needle or not records:
        return None
    needle_l = needle.lower()
    # exact match first
    for k, v in records.items():
        if k.lower() == needle_l:
            return v
    # contains match
    for k, v in records.items():
        if needle_l in k.lower() or k.lower() in needle_l:
            return v
    return None


def _match_schedule(
    away: Optional[str], home: Optional[str], schedule: List[Dict]
) -> Optional[Dict]:
    if not (away or home):
        return None
    for g in schedule:
        teams = g.get("teams", {})
        a = (teams.get("away", {}).get("team", {}).get("name") or "").lower()
        h = (teams.get("home", {}).get("team", {}).get("name") or "").lower()
        if away and home and away.lower() in a and home.lower() in h:
            return g
        if away and home and home.lower() in h:
            # away match is fuzzier than home; allow it as a secondary signal
            if away.lower().split()[-1] in a:
                return g
    return None


# --------------------------------------------------------------------------- #
# Weather
# --------------------------------------------------------------------------- #

async def fetch_weather(
    client: httpx.AsyncClient, lat: float, lon: float, when: datetime
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (wind_mph, wind_dir_deg, temperature_c) at the requested hour."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "mph",
        "timezone": "UTC",
        "forecast_days": 3,
    }
    try:
        r = await client.get(OPEN_METEO_BASE, params=params, timeout=10.0)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        logger.warning("Open-Meteo fetch failed: %s", e)
        return None, None, None

    hourly = data.get("hourly", {})
    times = hourly.get("time") or []
    if not times:
        return None, None, None

    # Find closest hour
    target = when.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    target_iso = target.strftime("%Y-%m-%dT%H:%M")
    try:
        idx = next(i for i, t in enumerate(times) if t.startswith(target_iso[:13]))
    except StopIteration:
        idx = 0

    def _safe(arr_name: str) -> Optional[float]:
        arr = hourly.get(arr_name)
        if not arr or idx >= len(arr):
            return None
        try:
            return float(arr[idx])
        except (TypeError, ValueError):
            return None

    return _safe("wind_speed_10m"), _safe("wind_direction_10m"), _safe("temperature_2m")


# --------------------------------------------------------------------------- #
# Public orchestrator
# --------------------------------------------------------------------------- #

async def build_context(
    client: httpx.AsyncClient,
    *,
    away_team: Optional[str],
    home_team: Optional[str],
    start_time: Optional[datetime],
    records: Dict[str, TeamForm],
    schedule: List[Dict],
) -> GameContext:
    """Assemble a GameContext for a single game. All sub-fetches are best
    effort — missing pieces simply mean less confidence later, not failure."""
    ctx = GameContext(venue=None)
    ctx.away_form = _match_team(away_team, records)
    ctx.home_form = _match_team(home_team, records)

    sched = _match_schedule(away_team, home_team, schedule)
    if sched:
        teams = sched.get("teams", {})
        ap = teams.get("away", {}).get("probablePitcher") or {}
        hp = teams.get("home", {}).get("probablePitcher") or {}
        ctx.away_pitcher = ap.get("fullName")
        ctx.home_pitcher = hp.get("fullName")
        # ERA is hydrated via a follow-up call when needed; we skip it here to
        # keep scheduling cheap. Add `season` hydrate later if you want it.
        venue = sched.get("venue", {}).get("name")
        ctx.venue = venue
        if venue and venue in VENUE_COORDS and start_time is not None:
            lat, lon = VENUE_COORDS[venue]
            wind, wind_dir, temp = await fetch_weather(client, lat, lon, start_time)
            ctx.wind_mph = wind
            ctx.wind_dir_deg = wind_dir
            ctx.temperature_c = temp

    return ctx
