"""Value detection — turn a Polymarket Game + GameContext into ranked plays.

For each binary market:

  edge_pp     = (p_fair - p_mkt) * 100              # percentage-point edge
  ev_per_$    = (p_fair / p_mkt) - 1                # expected return on $1 YES buy
  kelly_frac  = (b*p - q) / b   where b = (1-p_mkt)/p_mkt, q = 1-p_fair

We use a *fractional* Kelly (default 0.25 of full Kelly) because:
  * Our fair-prob estimate has model error
  * Polymarket spreads can move, so realized fill ≠ mid
  * Variance with full Kelly is brutal even when EV is real

Position size is reported as a percentage of bankroll. Tiers (S/A/B/C) bucket
plays by quality so the UI can show a clean sorted list.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from analysis.enrichment import GameContext, TeamForm
from polymarket.parser import Game, Market, MarketKind, Outcome

logger = logging.getLogger(__name__)


# Fraction of full Kelly to actually recommend. 0.25 is the standard
# "quarter Kelly" used by professional bettors to control variance.
KELLY_FRACTION = 0.25

# Hard caps on position size as % of bankroll, regardless of model output.
MAX_POSITION_PCT = 5.0     # never recommend more than 5% on a single play
MIN_POSITION_PCT = 0.5     # below this, the play isn't worth the friction


class Tier(str, Enum):
    S = "S"  # premium: high edge + high confidence
    A = "A"  # strong
    B = "B"  # decent
    C = "C"  # marginal


@dataclass
class ValuePlay:
    game: Game
    market: Market
    outcome: Outcome             # the side we like (YES on this label)
    p_market: float              # market-implied probability (0..1)
    p_fair: float                # our fair-probability estimate (0..1)
    edge_pp: float               # (p_fair - p_market) * 100
    ev_per_dollar: float         # expected $ return per $1 staked at YES price
    kelly_full_pct: float        # full Kelly stake as % of bankroll
    kelly_recommended_pct: float # KELLY_FRACTION × full Kelly, capped
    confidence: int              # 0..100
    tier: Tier
    rationale: List[str]

    @property
    def american_odds(self) -> int:
        """Convert market price to American moneyline odds for familiarity."""
        p = max(0.01, min(0.99, self.p_market))
        if p >= 0.5:
            return -int(round(p / (1 - p) * 100))
        return int(round((1 - p) / p * 100))

    @property
    def decimal_odds(self) -> float:
        return 1.0 / max(0.01, min(0.99, self.p_market))

    def position_for_bankroll(self, bankroll: float) -> float:
        """Dollar amount to stake given a bankroll."""
        return round(bankroll * self.kelly_recommended_pct / 100, 2)


# --------------------------------------------------------------------------- #
# Fair-probability estimators
# --------------------------------------------------------------------------- #

HOME_FIELD_PP = 0.04


def _blend_team_prob(form: TeamForm) -> float:
    if form.games < 10:
        return 0.6 * form.pythag_win_pct + 0.4 * form.win_pct
    return 0.5 * form.pythag_win_pct + 0.5 * form.win_pct


def fair_moneyline(
    *, target_team: str, ctx: GameContext, home_team: Optional[str]
) -> Optional[float]:
    if not ctx.away_form or not ctx.home_form:
        return None

    away_p = _blend_team_prob(ctx.away_form)
    home_p = _blend_team_prob(ctx.home_form)

    # log5: P(A beats B) = (pA - pA*pB) / (pA + pB - 2*pA*pB)
    denom = away_p + home_p - 2 * away_p * home_p
    if denom <= 0:
        return None
    p_away_wins = (away_p - away_p * home_p) / denom

    p_home_wins = min(0.95, (1.0 - p_away_wins) + HOME_FIELD_PP)
    p_away_wins = 1.0 - p_home_wins

    target_l = (target_team or "").lower()
    if home_team and target_l in home_team.lower():
        return p_home_wins
    return p_away_wins


def fair_runline_cover(p_moneyline: float, *, line: float) -> float:
    p = max(0.01, min(0.99, p_moneyline))
    if line > 0:
        return p ** 1.6
    return 1.0 - (1.0 - p) ** 1.6


def fair_total_over(line: float, ctx: GameContext) -> float:
    league_avg = 8.5
    expected = league_avg
    if ctx.away_form and ctx.home_form:
        rs = (ctx.away_form.runs_per_game + ctx.home_form.runs_per_game) / 2
        ra_est = ((ctx.away_form.runs_allowed / max(1, ctx.away_form.games))
                  + (ctx.home_form.runs_allowed / max(1, ctx.home_form.games))) / 2
        expected = 0.5 * (rs + ra_est) * 2

    if ctx.wind_mph is not None and ctx.wind_mph > 12:
        expected += 0.3
    if ctx.temperature_c is not None and ctx.temperature_c < 10:
        expected -= 0.3

    z = (expected - line) / 1.8
    return 1.0 / (1.0 + math.exp(-z))


# --------------------------------------------------------------------------- #
# Kelly sizing
# --------------------------------------------------------------------------- #

def kelly_fraction(p_fair: float, p_market: float) -> float:
    """Full-Kelly stake as a fraction of bankroll (0..1).

    For a binary contract bought at price p_market that pays $1 if it wins:
      b = (1 - p_market) / p_market    # net odds received per unit staked
      f* = (b * p_fair - (1 - p_fair)) / b
    """
    p = max(0.01, min(0.99, p_market))
    q = max(0.01, min(0.99, p_fair))
    b = (1.0 - p) / p
    if b <= 0:
        return 0.0
    f = (b * q - (1.0 - q)) / b
    return max(0.0, min(1.0, f))


def _classify_tier(edge_pp: float, confidence: int) -> Tier:
    if confidence >= 75 and edge_pp >= 7:
        return Tier.S
    if confidence >= 65 and edge_pp >= 5:
        return Tier.A
    if confidence >= 55 and edge_pp >= 3.5:
        return Tier.B
    return Tier.C


# --------------------------------------------------------------------------- #
# Confidence scoring
# --------------------------------------------------------------------------- #

def _liquidity_score(liq: float) -> int:
    if liq <= 0:
        return 0
    return min(35, int(7 * math.log10(liq + 1)))


def _spread_score(market: Market) -> int:
    if not market.is_binary:
        return 0
    s = market.outcomes[0].price + market.outcomes[1].price
    diff = abs(1.0 - s)
    if diff < 0.01:
        return 20
    if diff < 0.03:
        return 15
    if diff < 0.06:
        return 8
    return 2


def _edge_score(edge_pp: float) -> int:
    e = abs(edge_pp)
    if e < 1:
        return 0
    if e <= 12:
        return min(25, int(e * 2.2))
    return max(5, 25 - int((e - 12) * 1.5))


def _data_score(ctx: GameContext, kind: MarketKind) -> int:
    score = 0
    if ctx.away_form and ctx.away_form.games >= 20:
        score += 6
    if ctx.home_form and ctx.home_form.games >= 20:
        score += 6
    if kind == MarketKind.TOTAL and ctx.wind_mph is not None:
        score += 4
    if ctx.away_pitcher and ctx.home_pitcher:
        score += 4
    return min(20, score)


# --------------------------------------------------------------------------- #
# Per-market evaluators
# --------------------------------------------------------------------------- #

def _evaluate_moneyline(
    game: Game, market: Market, ctx: GameContext, min_edge_pp: float
) -> List[ValuePlay]:
    plays: List[ValuePlay] = []
    for o in market.outcomes:
        p_fair = fair_moneyline(target_team=o.label, ctx=ctx, home_team=game.home_team)
        if p_fair is None:
            continue
        play = _maybe_play(game, market, o, p_fair, ctx, min_edge_pp)
        if play:
            plays.append(play)
    return plays


def _evaluate_runline(
    game: Game, market: Market, ctx: GameContext, min_edge_pp: float
) -> List[ValuePlay]:
    if market.line is None:
        return []
    plays: List[ValuePlay] = []
    for o in market.outcomes:
        label_l = o.label.lower()
        team_name = label_l.replace(" -1.5", "").replace(" +1.5", "").strip()
        is_fav_cover = "-1.5" in label_l or "cover" in label_l
        line_signed = abs(market.line) if is_fav_cover else -abs(market.line)

        p_ml = fair_moneyline(target_team=team_name, ctx=ctx, home_team=game.home_team)
        if p_ml is None:
            continue
        p_fair = fair_runline_cover(p_ml, line=line_signed)
        play = _maybe_play(game, market, o, p_fair, ctx, min_edge_pp)
        if play:
            plays.append(play)
    return plays


def _evaluate_total(
    game: Game, market: Market, ctx: GameContext, min_edge_pp: float
) -> List[ValuePlay]:
    if market.line is None:
        return []
    p_over = fair_total_over(market.line, ctx)
    plays: List[ValuePlay] = []
    for o in market.outcomes:
        is_over = "over" in o.label.lower()
        p_fair = p_over if is_over else (1.0 - p_over)
        play = _maybe_play(game, market, o, p_fair, ctx, min_edge_pp)
        if play:
            plays.append(play)
    return plays


def _maybe_play(
    game: Game,
    market: Market,
    outcome: Outcome,
    p_fair: float,
    ctx: GameContext,
    min_edge_pp: float,
) -> Optional[ValuePlay]:
    p_mkt = max(0.01, min(0.99, outcome.price))
    edge = (p_fair - p_mkt) * 100
    if edge < min_edge_pp:
        return None

    ev = (p_fair / p_mkt) - 1.0

    confidence = (
        _liquidity_score(market.liquidity)
        + _spread_score(market)
        + _edge_score(edge)
        + _data_score(ctx, market.kind)
    )
    confidence = max(0, min(100, confidence))

    full_kelly = kelly_fraction(p_fair, p_mkt) * 100  # as %
    recommended = full_kelly * KELLY_FRACTION
    # Confidence acts as a further haircut — at confidence 50 we use half the
    # Kelly amount we'd otherwise recommend.
    recommended *= (confidence / 100.0)
    recommended = min(MAX_POSITION_PCT, recommended)
    if recommended < MIN_POSITION_PCT:
        recommended = 0.0   # don't recommend a position too small to matter

    tier = _classify_tier(edge, confidence)

    rationale: List[str] = []
    rationale.append(f"Market mid {p_mkt:.1%}, fair est {p_fair:.1%} → +{edge:.1f}pp edge")
    rationale.append(f"Liquidity ${market.liquidity:,.0f}")
    if ctx.away_form and ctx.home_form:
        rationale.append(
            f"{ctx.away_form.name} {ctx.away_form.wins}-{ctx.away_form.losses} "
            f"@ {ctx.home_form.name} {ctx.home_form.wins}-{ctx.home_form.losses}"
        )
    if market.kind == MarketKind.TOTAL and ctx.wind_mph is not None:
        rationale.append(f"Wind {ctx.wind_mph:.0f} mph at {ctx.venue or 'venue'}")
    if ctx.away_pitcher or ctx.home_pitcher:
        rationale.append(f"SP: {ctx.away_pitcher or '?'} vs {ctx.home_pitcher or '?'}")

    return ValuePlay(
        game=game,
        market=market,
        outcome=outcome,
        p_market=p_mkt,
        p_fair=p_fair,
        edge_pp=edge,
        ev_per_dollar=ev,
        kelly_full_pct=full_kelly,
        kelly_recommended_pct=recommended,
        confidence=confidence,
        tier=tier,
        rationale=rationale,
    )


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #

def evaluate_game(
    game: Game, ctx: GameContext, *, min_edge_pp: float = 4.0
) -> List[ValuePlay]:
    out: List[ValuePlay] = []
    for m in game.markets:
        if not m.is_binary or m.liquidity <= 0:
            continue
        if m.kind == MarketKind.MONEYLINE:
            out.extend(_evaluate_moneyline(game, m, ctx, min_edge_pp))
        elif m.kind == MarketKind.RUNLINE:
            out.extend(_evaluate_runline(game, m, ctx, min_edge_pp))
        elif m.kind == MarketKind.TOTAL:
            out.extend(_evaluate_total(game, m, ctx, min_edge_pp))
    return out


def rank_plays(plays: List[ValuePlay]) -> List[ValuePlay]:
    """Sort by tier first, then by EV-weighted confidence."""
    tier_rank = {Tier.S: 0, Tier.A: 1, Tier.B: 2, Tier.C: 3}
    return sorted(
        plays,
        key=lambda p: (tier_rank[p.tier], -p.confidence * (1.0 + p.ev_per_dollar)),
    )
