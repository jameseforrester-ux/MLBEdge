"""Rich text formatting for the bot UI.

Telegram's HTML supports a small set of tags (b, i, u, s, code, pre, a) and
emoji. We use those plus careful spacing to make the messages look like proper
betting cards rather than a wall of text.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from analysis.value import Tier, ValuePlay
from polymarket.parser import Game, MarketKind


# --------------------------------------------------------------------------- #
# Emoji palette
# --------------------------------------------------------------------------- #

TIER_EMOJI = {
    Tier.S: "🌟",
    Tier.A: "💪",
    Tier.B: "✓",
    Tier.C: "•",
}

TIER_LABEL = {
    Tier.S: "S — Premium",
    Tier.A: "A — Strong",
    Tier.B: "B — Decent",
    Tier.C: "C — Marginal",
}

KIND_EMOJI = {
    MarketKind.MONEYLINE: "💰",
    MarketKind.RUNLINE: "📏",
    MarketKind.TOTAL: "🎯",
    MarketKind.OTHER: "❓",
}

KIND_LABEL = {
    MarketKind.MONEYLINE: "Moneyline",
    MarketKind.RUNLINE: "Run Line",
    MarketKind.TOTAL: "Total Runs",
    MarketKind.OTHER: "Other",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def fmt_pct(p: float) -> str:
    return f"{p * 100:.1f}%"


def fmt_money(amount: float) -> str:
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.1f}k"
    return f"${amount:.0f}"


def fmt_american(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def progress_bar(value: int, *, width: int = 10, max_value: int = 100) -> str:
    """Unicode progress bar for confidence scores."""
    value = max(0, min(max_value, value))
    filled = int(round(value / max_value * width))
    return "█" * filled + "░" * (width - filled)


def short_slug(slug: str, *, max_len: int = 38) -> str:
    if len(slug) <= max_len:
        return slug
    return slug[: max_len - 1] + "…"


# --------------------------------------------------------------------------- #
# Game card (used by /games and the games menu)
# --------------------------------------------------------------------------- #

def render_game_card(game: Game) -> str:
    """One-game summary showing all three market types if present."""
    when = game.start_time.strftime("%a %b %-d · %H:%M UTC") if game.start_time else "TBD"
    header = f"⚾ <b>{game.matchup}</b>\n<i>{when}</i>"

    rows: List[str] = []

    ml = game.market(MarketKind.MONEYLINE)
    if ml and ml.is_binary:
        a, b = ml.outcomes
        rows.append(
            f"💰 <b>ML</b>  "
            f"{a.label} <code>{fmt_pct(a.price)}</code>  ·  "
            f"{b.label} <code>{fmt_pct(b.price)}</code>"
        )

    rl = game.market(MarketKind.RUNLINE)
    if rl and rl.is_binary and rl.line is not None:
        a, b = rl.outcomes
        rows.append(
            f"📏 <b>RL ±{abs(rl.line)}</b>  "
            f"{a.label} <code>{fmt_pct(a.price)}</code>  ·  "
            f"{b.label} <code>{fmt_pct(b.price)}</code>"
        )

    tot = game.market(MarketKind.TOTAL)
    if tot and tot.is_binary and tot.line is not None:
        a, b = tot.outcomes
        rows.append(
            f"🎯 <b>O/U {tot.line}</b>  "
            f"{a.label} <code>{fmt_pct(a.price)}</code>  ·  "
            f"{b.label} <code>{fmt_pct(b.price)}</code>"
        )

    body = "\n".join(rows) if rows else "<i>No liquid markets</i>"
    footer = f"<code>{short_slug(game.slug)}</code>"
    return f"{header}\n{body}\n{footer}"


def render_games_list(games: List[Game], *, max_games: int = 12) -> str:
    if not games:
        return "🚫 <b>No active MLB markets</b>\n\nPolymarket has no liquid MLB games right now. Check back closer to first pitch."

    cards = [render_game_card(g) for g in games[:max_games]]
    body = "\n\n──────\n\n".join(cards)
    note = ""
    if len(games) > max_games:
        note = f"\n\n<i>Showing {max_games} of {len(games)} games. Use /value to see top plays only.</i>"
    return f"⚾ <b>MLB Markets — Live on Polymarket</b>\n\n{body}{note}"


# --------------------------------------------------------------------------- #
# Play card (used by /value and alerts)
# --------------------------------------------------------------------------- #

def render_play_card(play: ValuePlay, *, bankroll: Optional[float] = None) -> str:
    """A single value play, formatted as a betting card."""
    g = play.game
    m = play.market
    o = play.outcome

    tier_badge = f"{TIER_EMOJI[play.tier]} <b>Tier {play.tier.value}</b>"
    when = g.start_time.strftime("%a %b %-d · %H:%M UTC") if g.start_time else "TBD"

    # Header
    header_lines = [
        f"{tier_badge}  ·  {KIND_EMOJI[m.kind]} <b>{KIND_LABEL[m.kind]}</b>",
        f"⚾ <b>{g.matchup}</b>",
        f"<i>{when}</i>",
    ]

    # Pick block
    pick_block = [
        "",
        f"🎯 <b>PICK:</b> <b>{o.label}</b> @ <code>{fmt_pct(play.p_market)}</code>  "
        f"<i>({fmt_american(play.american_odds)})</i>",
    ]

    # Stats grid
    stats = [
        "",
        "<b>📊 Analysis</b>",
        f"  Implied prob:    <code>{fmt_pct(play.p_market)}</code>",
        f"  Fair prob:       <code>{fmt_pct(play.p_fair)}</code>",
        f"  Edge:            <code>+{play.edge_pp:.2f}pp</code>",
        f"  Expected Value:  <code>+{play.ev_per_dollar * 100:.2f}%</code>",
    ]

    # Sizing
    sizing = [
        "",
        "<b>💵 Position Sizing (¼-Kelly)</b>",
        f"  Full Kelly:      <code>{play.kelly_full_pct:.2f}%</code> of bankroll",
        f"  Recommended:     <code>{play.kelly_recommended_pct:.2f}%</code> of bankroll",
    ]
    if bankroll and bankroll > 0:
        dollars = play.position_for_bankroll(bankroll)
        if dollars > 0:
            sizing.append(
                f"  At {fmt_money(bankroll)} bankroll: <b>{fmt_money(dollars)}</b>"
            )
        else:
            sizing.append(f"  At {fmt_money(bankroll)} bankroll: <i>skip (too small)</i>")

    # Confidence bar
    conf_bar = progress_bar(play.confidence, width=12)
    confidence = [
        "",
        f"<b>🎲 Confidence:</b> <code>{play.confidence}/100</code>",
        f"  <code>{conf_bar}</code>",
    ]

    # Rationale
    rationale = ["", "<b>📝 Rationale</b>"]
    for r in play.rationale:
        rationale.append(f"  • {r}")

    # Footer
    footer = [
        "",
        f'<a href="https://polymarket.com/event/{g.slug}">→ Open on Polymarket</a>',
    ]

    return "\n".join(header_lines + pick_block + stats + sizing + confidence + rationale + footer)


def render_play_summary(play: ValuePlay, *, index: int) -> str:
    """Compact one-liner used in lists."""
    return (
        f"{TIER_EMOJI[play.tier]} <b>#{index}</b>  "
        f"{play.outcome.label} ({KIND_LABEL[play.market.kind]})  ·  "
        f"<code>+{play.edge_pp:.1f}pp</code>  ·  "
        f"size <code>{play.kelly_recommended_pct:.1f}%</code>  ·  "
        f"conf <code>{play.confidence}</code>"
    )


def render_value_summary(plays: List[ValuePlay], *, bankroll: Optional[float] = None) -> str:
    """Header line + tier breakdown for a /value response."""
    if not plays:
        return (
            "📭 <b>No value plays right now</b>\n\n"
            "Either the market is efficient at the moment, or your filters are "
            "tight. Try lowering /edge or /conf in <b>⚙️ Settings</b>."
        )

    tier_counts: dict = {Tier.S: 0, Tier.A: 0, Tier.B: 0, Tier.C: 0}
    for p in plays:
        tier_counts[p.tier] += 1

    lines = [
        "🎯 <b>Value Plays Detected</b>",
        "",
        f"Found <b>{len(plays)}</b> plays passing your filters:",
    ]
    for tier in (Tier.S, Tier.A, Tier.B, Tier.C):
        if tier_counts[tier]:
            lines.append(f"  {TIER_EMOJI[tier]} {TIER_LABEL[tier]}: <b>{tier_counts[tier]}</b>")

    if bankroll:
        lines.append("")
        lines.append(f"<i>Sizing shown for {fmt_money(bankroll)} bankroll.</i>")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Settings + main menu
# --------------------------------------------------------------------------- #

def render_main_menu() -> str:
    return (
        "⚾ <b>Polymarket MLB Value Bot</b>\n\n"
        "I scan today's MLB markets on Polymarket — moneylines, run lines, and "
        "totals — then surface the spots where the live price looks too far from "
        "a fair-probability estimate.\n\n"
        "Use the buttons below or these commands anytime:\n"
        "  /games · /value · /tracked · /settings · /help"
    )


def render_settings(prefs) -> str:
    alerts = "🔔 ON" if prefs.value_alerts else "🔕 OFF"
    return (
        "⚙️ <b>Your Settings</b>\n\n"
        f"<b>Bankroll</b>             <code>{fmt_money(prefs.bankroll)}</code>\n"
        f"<b>Min edge</b>              <code>{prefs.min_edge_pct:.1f}pp</code>\n"
        f"<b>Min confidence</b>        <code>{prefs.min_confidence}/100</code>\n"
        f"<b>Background alerts</b>     {alerts}\n"
        "\n<i>Tap a button below to change any setting, or use the matching "
        "command directly (e.g. /edge 5).</i>"
    )


def render_help() -> str:
    return (
        "📖 <b>How this bot works</b>\n\n"
        "<b>The data:</b> Polymarket's live order books for MLB games. Prices are "
        "implied probabilities, not American odds — a YES at 0.55 means the market "
        "thinks that outcome is 55% likely.\n\n"
        "<b>The model:</b> For each market we compute a <i>fair probability</i> "
        "from team records (with Pythagorean expectation), home-field, and for "
        "totals a wind/temperature adjustment when available.\n\n"
        "<b>The edge:</b> <code>fair − market</code> in percentage points. Plays "
        "below your /edge threshold get filtered out.\n\n"
        "<b>The confidence score</b> rolls up liquidity, bid/ask spread, edge "
        "magnitude, and how much real data we had on the teams.\n\n"
        "<b>Position sizing</b> uses ¼-Kelly, then haircuts further by your "
        "confidence score, and is hard-capped at 5% of bankroll. If the recommended "
        "stake comes out below 0.5%, we tell you to skip.\n\n"
        "<b>Tiers:</b>\n"
        f"  {TIER_EMOJI[Tier.S]} <b>S</b> — confidence ≥ 75 and edge ≥ 7pp\n"
        f"  {TIER_EMOJI[Tier.A]} <b>A</b> — confidence ≥ 65 and edge ≥ 5pp\n"
        f"  {TIER_EMOJI[Tier.B]} <b>B</b> — confidence ≥ 55 and edge ≥ 3.5pp\n"
        f"  {TIER_EMOJI[Tier.C]} <b>C</b> — anything else passing your filters\n\n"
        "<b>Disclaimer:</b> This is informational only. Polymarket is a prediction "
        "market and the model has obvious limits (no pitcher quality, simple "
        "weather model, no umpire data). Treat outputs as one input, not gospel."
    )


# --------------------------------------------------------------------------- #
# Misc messages
# --------------------------------------------------------------------------- #

def render_loading(message: str) -> str:
    return f"⏳ <i>{message}</i>"


def render_error(message: str) -> str:
    return f"⚠️ <b>Error:</b> {message}"


def render_tracked_list(items: Iterable) -> str:
    items = list(items)
    if not items:
        return (
            "📋 <b>Tracked games</b>\n\n"
            "<i>You're not tracking any games yet.</i>\n\n"
            "Browse /games and tap <b>👁 Track</b> on any game to get alerts when "
            "its prices move."
        )
    lines = ["📋 <b>Tracked games</b>", ""]
    for t in items:
        lines.append(f"• <code>{short_slug(t.event_slug, max_len=44)}</code>")
        lines.append(f"  Alert at ±{t.threshold_pp:.1f}pp")
    return "\n".join(lines)
