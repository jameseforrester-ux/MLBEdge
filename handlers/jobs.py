"""Scheduled background jobs.

Two jobs run on the python-telegram-bot JobQueue:

* `tracked_price_job` — every TRACK_REFRESH_SECONDS, refresh prices for any
  game that any user is tracking. If a YES-token midpoint moved ≥ threshold_pp
  since we last alerted that user, ping them.

* `value_scan_job` — every VALUE_SCAN_SECONDS, run a full /value scan for each
  user who has alerts on. Send any new tier-S/A plays they haven't been
  notified about yet.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date
from typing import Dict, List, Tuple

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from analysis.value import Tier, ValuePlay, evaluate_game, rank_plays
from handlers import keyboards as kb
from handlers import ui
from polymarket.parser import (
    Game,
    apply_live_prices,
    parse_event,
    parse_events,
    upcoming_only,
)

logger = logging.getLogger(__name__)


def _bot_ctx(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["ctx"]


# --------------------------------------------------------------------------- #
# Tracked-game price alerts
# --------------------------------------------------------------------------- #

async def tracked_price_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _bot_ctx(context)
    tracked = bc.store.all_tracked()
    if not tracked:
        return

    # Group tracked games by slug so we only fetch each event once.
    by_slug: Dict[str, List] = defaultdict(list)
    for t in tracked:
        by_slug[t.event_slug].append(t)

    logger.info("Refreshing %d tracked games for %d watchers", len(by_slug), len(tracked))

    for slug, watchers in by_slug.items():
        try:
            raw = await bc.poly.get_event_by_slug(slug)
            if not raw:
                continue
            game = parse_event(raw)
            if not game:
                continue
            tids = [o.token_id for m in game.markets for o in m.outcomes if o.token_id]
            if tids:
                mids = await bc.poly.get_midpoints(tids)
                apply_live_prices(game, mids)

            for w in watchers:
                await _check_one_watcher(context, w, game)
        except Exception:
            logger.exception("tracked refresh failed for %s", slug)


async def _check_one_watcher(context: ContextTypes.DEFAULT_TYPE, w, game: Game) -> None:
    bc = _bot_ctx(context)
    current: Dict[str, float] = {}
    for m in game.markets:
        for o in m.outcomes:
            if o.token_id and 0.0 <= o.price <= 1.0:
                current[o.token_id] = o.price

    last = w.last_seen_prices or {}
    movers: List[Tuple[str, float, float]] = []   # (label, old, new)
    threshold = w.threshold_pp / 100.0

    for m in game.markets:
        for o in m.outcomes:
            new_p = current.get(o.token_id)
            old_p = last.get(o.token_id)
            if new_p is None:
                continue
            if old_p is None:
                continue
            if abs(new_p - old_p) >= threshold:
                movers.append((f"{o.label} ({m.kind.value})", old_p, new_p))

    # Always update last-seen so we don't fire on every cycle of slow drift.
    bc.store.update_tracked_prices(w.chat_id, w.event_slug, current)

    if not movers:
        return

    # Build alert
    lines = [
        "📈 <b>Price movement</b>",
        f"⚾ <b>{game.matchup}</b>",
        "",
    ]
    for label, old, new in movers:
        delta = (new - old) * 100
        arrow = "▲" if delta > 0 else "▼"
        lines.append(
            f"  {arrow} <b>{label}</b>: "
            f"<code>{old:.1%}</code> → <code>{new:.1%}</code> "
            f"<i>({'+' if delta > 0 else ''}{delta:.1f}pp)</i>"
        )
    lines.append("")
    lines.append(f'<a href="https://polymarket.com/event/{game.slug}">→ Open on Polymarket</a>')

    try:
        await context.bot.send_message(
            chat_id=w.chat_id,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Failed to send tracked alert to %s", w.chat_id)


# --------------------------------------------------------------------------- #
# Value-play scan alerts
# --------------------------------------------------------------------------- #

async def value_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _bot_ctx(context)
    users = bc.store.all_value_alert_users()
    if not users:
        return

    logger.info("Running value scan for %d alert subscribers", len(users))

    # Pull all MLB games once.
    raw = await bc.poly.get_mlb_events(limit=80)
    games = parse_events(raw)
    games = upcoming_only(games)

    if not games:
        return

    tids = [o.token_id for g in games for m in g.markets for o in m.outcomes if o.token_id]
    if tids:
        mids = await bc.poly.get_midpoints(tids)
        for g in games:
            apply_live_prices(g, mids)

    games = [
        g for g in games
        if any(m.liquidity >= bc.settings.min_liquidity_usdc for m in g.markets)
    ]

    # Build enriched contexts once (re-used across users to save calls).
    records = await bc.get_records()
    schedule = await bc.get_schedule(date.today())

    sem = asyncio.Semaphore(4)

    async def _ctx_for(g: Game):
        from analysis.enrichment import build_context
        async with sem:
            return await build_context(
                bc.http,
                away_team=g.away_team,
                home_team=g.home_team,
                start_time=g.start_time,
                records=records,
                schedule=schedule,
            )

    contexts = await asyncio.gather(*[_ctx_for(g) for g in games])
    game_ctx = list(zip(games, contexts))

    # Loop users — each may have different thresholds.
    for user in users:
        plays: List[ValuePlay] = []
        for g, gctx in game_ctx:
            plays.extend(evaluate_game(g, gctx, min_edge_pp=user.min_edge_pct))
        plays = rank_plays(plays)
        plays = [p for p in plays if p.confidence >= user.min_confidence and p.tier in (Tier.S, Tier.A)]

        for play in plays[:3]:   # at most 3 alerts per scan per user
            alert_key = f"value:{play.game.slug}:{play.outcome.token_id}:{play.tier.value}"
            if bc.store.already_sent(user.chat_id, alert_key):
                continue
            bc.store.mark_sent(user.chat_id, alert_key)
            try:
                await context.bot.send_message(
                    chat_id=user.chat_id,
                    text="🚨 <b>New value play</b>\n\n" + ui.render_play_card(play, bankroll=user.bankroll),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=kb.value_pager_keyboard(index=0, total=1, slug=play.game.slug),
                )
            except Exception:
                logger.exception("Failed to send value alert to %s", user.chat_id)
