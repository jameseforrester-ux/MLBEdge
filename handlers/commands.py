"""Telegram command + callback handlers.

The bot offers a menu-driven UI: most actions can be reached either by typing a
slash command or by tapping inline buttons. Both paths route through the same
`_render_*` functions to keep behavior consistent.

Per-chat state we cache in `chat_data` (provided by python-telegram-bot):

  * "plays"           — list[ValuePlay] from the latest /value scan, used for
                        pager navigation.
  * "games"           — list[Game] from the latest /games scan, used to map
                        slug → Game object on game:view callbacks.
  * "awaiting"        — set when we've prompted the user for free-form input
                        (e.g. bankroll). Cleared on the next message.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from analysis.enrichment import (
    GameContext,
    TeamForm,
    build_context,
    fetch_schedule,
    fetch_team_records,
)
from analysis.value import ValuePlay, evaluate_game, rank_plays
from config import Settings
from handlers import keyboards as kb
from handlers import ui
from polymarket.client import PolymarketClient
from polymarket.parser import (
    Game,
    apply_live_prices,
    parse_event,
    parse_events,
    upcoming_only,
)
from storage import Store

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Shared bot context
# --------------------------------------------------------------------------- #

@dataclass
class BotContext:
    """Shared state attached to the Application's bot_data."""
    settings: Settings
    poly: PolymarketClient
    store: Store
    http: httpx.AsyncClient

    _records: Optional[Dict[str, TeamForm]] = None
    _records_at: Optional[datetime] = None
    _schedule: Optional[List[Dict]] = None
    _schedule_for: Optional[date] = None
    _records_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _schedule_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get_records(self) -> Dict[str, TeamForm]:
        async with self._records_lock:
            now = datetime.now(timezone.utc)
            if (
                self._records is None
                or not self._records_at
                or (now - self._records_at).total_seconds() > 3600
            ):
                self._records = await fetch_team_records(self.http)
                self._records_at = now
            return self._records or {}

    async def get_schedule(self, on: date) -> List[Dict]:
        async with self._schedule_lock:
            if self._schedule is None or self._schedule_for != on:
                self._schedule = await fetch_schedule(self.http, on)
                self._schedule_for = on
            return self._schedule or []


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    return context.application.bot_data["ctx"]


def _is_allowed(update: Update, settings: Settings) -> bool:
    if not settings.allowed_user_ids:
        return True
    user = update.effective_user
    return bool(user and user.id in settings.allowed_user_ids)


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _is_allowed(update, _ctx(context).settings):
        chat = update.effective_chat
        if chat:
            await chat.send_message("This bot is private. Ask the operator to add your Telegram ID.")
        return False
    return True


# --------------------------------------------------------------------------- #
# Game / play loaders
# --------------------------------------------------------------------------- #

async def _load_mlb_games(bc: BotContext, *, limit: int = 50) -> List[Game]:
    raw = await bc.poly.get_mlb_events(limit=limit)
    games = parse_events(raw)
    games = upcoming_only(games)

    token_ids: List[str] = []
    for g in games:
        for m in g.markets:
            for o in m.outcomes:
                if o.token_id:
                    token_ids.append(o.token_id)

    if token_ids:
        midpoints = await bc.poly.get_midpoints(token_ids)
        for g in games:
            apply_live_prices(g, midpoints)

    games = [
        g for g in games
        if any(m.liquidity >= bc.settings.min_liquidity_usdc for m in g.markets)
    ]
    return games


async def _evaluate_all(bc: BotContext, games: List[Game], min_edge_pp: float) -> List[ValuePlay]:
    records = await bc.get_records()
    schedule = await bc.get_schedule(date.today())

    sem = asyncio.Semaphore(4)

    async def _one(g: Game) -> Tuple[Game, GameContext]:
        async with sem:
            ctx = await build_context(
                bc.http,
                away_team=g.away_team,
                home_team=g.home_team,
                start_time=g.start_time,
                records=records,
                schedule=schedule,
            )
            return g, ctx

    pairs = await asyncio.gather(*(_one(g) for g in games))

    plays: List[ValuePlay] = []
    for g, ctx in pairs:
        plays.extend(evaluate_game(g, ctx, min_edge_pp=min_edge_pp))
    return rank_plays(plays)


# --------------------------------------------------------------------------- #
# Render-and-reply helpers (used by both commands and callbacks)
# --------------------------------------------------------------------------- #

async def _safe_send_or_edit(
    update: Update, *, text: str, reply_markup=None
) -> None:
    """If we got here from a callback query, edit the existing message;
    otherwise send a new one."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            return
        except BadRequest as e:
            # "Message is not modified" → just answer the callback and move on
            if "not modified" in str(e).lower():
                await update.callback_query.answer()
                return
            logger.debug("edit_message_text failed, falling back to send: %s", e)
    chat = update.effective_chat
    if chat:
        await chat.send_message(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )


async def _render_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_send_or_edit(update, text=ui.render_main_menu(), reply_markup=kb.main_menu())


async def _render_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_send_or_edit(update, text=ui.render_help(), reply_markup=kb.back_to_menu())


async def _render_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    chat_id = update.effective_chat.id
    prefs = bc.store.get_prefs(
        chat_id,
        default_edge=bc.settings.default_min_edge_pct,
        default_conf=bc.settings.default_min_confidence,
    )
    await _safe_send_or_edit(
        update,
        text=ui.render_settings(prefs),
        reply_markup=kb.settings_keyboard(alerts_on=prefs.value_alerts),
    )


async def _render_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    chat = update.effective_chat
    if update.callback_query:
        await update.callback_query.answer("Loading games…")
    else:
        loading = await chat.send_message(
            ui.render_loading("Fetching MLB markets from Polymarket…"),
            parse_mode=ParseMode.HTML,
        )

    try:
        games = await _load_mlb_games(bc)
    except Exception as e:
        logger.exception("games failed")
        await _safe_send_or_edit(
            update, text=ui.render_error(str(e)), reply_markup=kb.back_to_menu()
        )
        return

    # Cache for game:view callbacks
    context.chat_data["games"] = {g.slug: g for g in games}

    text = ui.render_games_list(games)
    if len(text) > 3800:
        text = text[:3800] + "\n\n<i>(truncated)</i>"

    if update.callback_query:
        await _safe_send_or_edit(
            update, text=text, reply_markup=kb.games_list_keyboard([g.slug for g in games])
        )
    else:
        await loading.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=kb.games_list_keyboard([g.slug for g in games]),
        )


async def _render_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    chat = update.effective_chat
    chat_id = chat.id
    prefs = bc.store.get_prefs(
        chat_id,
        default_edge=bc.settings.default_min_edge_pct,
        default_conf=bc.settings.default_min_confidence,
    )

    if update.callback_query:
        await update.callback_query.answer("Scanning markets…")
        loading_msg = None
    else:
        loading_msg = await chat.send_message(
            ui.render_loading(
                f"Scanning MLB markets — edge ≥ {prefs.min_edge_pct:.1f}pp, "
                f"confidence ≥ {prefs.min_confidence}…"
            ),
            parse_mode=ParseMode.HTML,
        )

    try:
        games = await _load_mlb_games(bc)
        plays = await _evaluate_all(bc, games, min_edge_pp=prefs.min_edge_pct)
    except Exception as e:
        logger.exception("value scan failed")
        if loading_msg:
            await loading_msg.edit_text(
                ui.render_error(str(e)),
                parse_mode=ParseMode.HTML,
                reply_markup=kb.back_to_menu(),
            )
        else:
            await _safe_send_or_edit(
                update, text=ui.render_error(str(e)), reply_markup=kb.back_to_menu()
            )
        return

    plays = [p for p in plays if p.confidence >= prefs.min_confidence]
    context.chat_data["plays"] = plays

    if not plays:
        text = ui.render_value_summary(plays)
        if loading_msg:
            await loading_msg.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=kb.empty_value_keyboard(),
            )
        else:
            await _safe_send_or_edit(
                update, text=text, reply_markup=kb.empty_value_keyboard()
            )
        return

    # Show summary + first play
    summary = ui.render_value_summary(plays, bankroll=prefs.bankroll)
    top = plays[0]
    text = (
        summary
        + "\n\n──────\n\n"
        + ui.render_play_card(top, bankroll=prefs.bankroll)
    )
    keyboard = kb.value_pager_keyboard(index=0, total=len(plays), slug=top.game.slug)

    if loading_msg:
        await loading_msg.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    else:
        await _safe_send_or_edit(update, text=text, reply_markup=keyboard)


async def _render_play_at(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int) -> None:
    bc = _ctx(context)
    plays: List[ValuePlay] = context.chat_data.get("plays") or []
    if not plays:
        # Cache lost (process restart) — re-scan
        await _render_value(update, context)
        return
    idx = idx % len(plays)
    prefs = bc.store.get_prefs(
        update.effective_chat.id,
        default_edge=bc.settings.default_min_edge_pct,
        default_conf=bc.settings.default_min_confidence,
    )
    play = plays[idx]
    summary = ui.render_value_summary(plays, bankroll=prefs.bankroll)
    text = (
        summary
        + "\n\n──────\n\n"
        + ui.render_play_card(play, bankroll=prefs.bankroll)
    )
    keyboard = kb.value_pager_keyboard(index=idx, total=len(plays), slug=play.game.slug)
    await _safe_send_or_edit(update, text=text, reply_markup=keyboard)


async def _render_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bc = _ctx(context)
    items = bc.store.list_tracked(update.effective_chat.id)
    text = ui.render_tracked_list(items)
    keyboard = kb.tracked_keyboard([t.event_slug for t in items])
    await _safe_send_or_edit(update, text=text, reply_markup=keyboard)


async def _render_game_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, slug: str) -> None:
    bc = _ctx(context)
    chat_id = update.effective_chat.id

    # Try cache first
    cache = context.chat_data.get("games") or {}
    game: Optional[Game] = cache.get(slug)
    if game is None:
        raw = await bc.poly.get_event_by_slug(slug)
        if raw:
            game = parse_event(raw)
            if game:
                # Refresh prices
                tids = [o.token_id for m in game.markets for o in m.outcomes if o.token_id]
                if tids:
                    mids = await bc.poly.get_midpoints(tids)
                    apply_live_prices(game, mids)

    if not game:
        await _safe_send_or_edit(
            update,
            text=ui.render_error(f"Couldn't find event '{slug}'."),
            reply_markup=kb.back_to_menu(),
        )
        return

    is_tracked = any(t.event_slug == slug for t in bc.store.list_tracked(chat_id))
    await _safe_send_or_edit(
        update,
        text=ui.render_game_card(game),
        reply_markup=kb.game_detail_keyboard(slug, is_tracked=is_tracked),
    )


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _render_main_menu(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _render_help(update, context)


async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _render_games(update, context)


async def cmd_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _render_value(update, context)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _render_settings(update, context)


async def cmd_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _render_tracked(update, context)


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    bc = _ctx(context)
    if not context.args:
        await update.effective_chat.send_message(
            "Usage: <code>/track &lt;event-slug&gt;</code>\nGet the slug from /games.",
            parse_mode=ParseMode.HTML,
        )
        return
    slug = context.args[0].strip()
    raw = await bc.poly.get_event_by_slug(slug)
    if not raw:
        await update.effective_chat.send_message(
            ui.render_error(f"Couldn't find event '{slug}'."), parse_mode=ParseMode.HTML
        )
        return
    bc.store.track(update.effective_chat.id, slug)
    title = raw.get("title") or slug
    await update.effective_chat.send_message(
        f"✅ Now tracking <b>{title}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    bc = _ctx(context)
    if not context.args:
        await update.effective_chat.send_message(
            "Usage: <code>/untrack &lt;event-slug&gt;</code>", parse_mode=ParseMode.HTML
        )
        return
    slug = context.args[0].strip()
    removed = bc.store.untrack(update.effective_chat.id, slug)
    msg = "✅ Stopped tracking." if removed else "🤷 You weren't tracking that one."
    await update.effective_chat.send_message(msg)


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    bc = _ctx(context)
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_chat.send_message(
            "Usage: <code>/alerts on</code> or <code>/alerts off</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    on = context.args[0].lower() == "on"
    bc.store.set_pref(update.effective_chat.id, value_alerts=1 if on else 0)
    msg = "🔔 Background alerts <b>ON</b>." if on else "🔕 Background alerts <b>OFF</b>."
    await update.effective_chat.send_message(msg, parse_mode=ParseMode.HTML)


async def cmd_edge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    bc = _ctx(context)
    if not context.args:
        await update.effective_chat.send_message(
            "Usage: <code>/edge 4.5</code>", parse_mode=ParseMode.HTML
        )
        return
    try:
        v = float(context.args[0])
    except ValueError:
        await update.effective_chat.send_message("Edge must be a number, e.g. 4.5")
        return
    v = max(0.5, min(30.0, v))
    bc.store.set_pref(update.effective_chat.id, min_edge_pct=v)
    await update.effective_chat.send_message(f"✅ Min edge set to {v:.1f}pp.")


async def cmd_conf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    bc = _ctx(context)
    if not context.args:
        await update.effective_chat.send_message(
            "Usage: <code>/conf 60</code>", parse_mode=ParseMode.HTML
        )
        return
    try:
        v = int(context.args[0])
    except ValueError:
        await update.effective_chat.send_message("Confidence must be an integer 0-100.")
        return
    v = max(0, min(100, v))
    bc.store.set_pref(update.effective_chat.id, min_confidence=v)
    await update.effective_chat.send_message(f"✅ Min confidence set to {v}.")


async def cmd_bankroll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    bc = _ctx(context)
    if not context.args:
        await update.effective_chat.send_message(
            "Usage: <code>/bankroll 5000</code>", parse_mode=ParseMode.HTML
        )
        return
    try:
        v = float(context.args[0].replace(",", "").replace("$", ""))
    except ValueError:
        await update.effective_chat.send_message("Bankroll must be a number, e.g. 5000")
        return
    v = max(10.0, min(10_000_000.0, v))
    bc.store.set_pref(update.effective_chat.id, bankroll=v)
    await update.effective_chat.send_message(f"✅ Bankroll set to ${v:,.2f}.")


# --------------------------------------------------------------------------- #
# Inline-button callbacks
# --------------------------------------------------------------------------- #

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    q = update.callback_query
    if not q or not q.data:
        return

    data = q.data
    parts = data.split(":", 2)
    namespace = parts[0]

    bc = _ctx(context)
    chat_id = update.effective_chat.id

    try:
        # ----- main menu --------------------------------------------------- #
        if namespace == "menu":
            target = parts[1] if len(parts) > 1 else "main"
            if target == "main":
                await q.answer()
                await _render_main_menu(update, context)
            elif target == "games":
                await _render_games(update, context)
            elif target == "value":
                await _render_value(update, context)
            elif target == "tracked":
                await q.answer()
                await _render_tracked(update, context)
            elif target == "settings":
                await q.answer()
                await _render_settings(update, context)
            elif target == "help":
                await q.answer()
                await _render_help(update, context)

        # ----- settings buttons ------------------------------------------- #
        elif namespace == "set":
            sub = parts[1] if len(parts) > 1 else ""
            arg = parts[2] if len(parts) > 2 else ""
            if sub == "edge":
                try:
                    v = float(arg)
                    bc.store.set_pref(chat_id, min_edge_pct=v)
                    await q.answer(f"Min edge → {v:.1f}pp")
                except ValueError:
                    await q.answer("Bad value", show_alert=True)
                await _render_settings(update, context)
            elif sub == "conf":
                try:
                    v = int(arg)
                    bc.store.set_pref(chat_id, min_confidence=v)
                    await q.answer(f"Min confidence → {v}")
                except ValueError:
                    await q.answer("Bad value", show_alert=True)
                await _render_settings(update, context)
            elif sub == "alerts":
                on = (arg == "on")
                bc.store.set_pref(chat_id, value_alerts=1 if on else 0)
                await q.answer("Alerts ON" if on else "Alerts OFF")
                await _render_settings(update, context)
            elif sub == "bankroll":
                # Prompt for free-form input
                context.chat_data["awaiting"] = "bankroll"
                await q.answer()
                await update.effective_chat.send_message(
                    "💵 <b>Set bankroll</b>\n\n"
                    "Reply with the dollar amount you want to size positions against "
                    "(e.g. <code>5000</code>). Position recommendations will be a "
                    "percentage of this number.",
                    parse_mode=ParseMode.HTML,
                )

        # ----- value pager ------------------------------------------------ #
        elif namespace == "play":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "noop":
                await q.answer()
            elif sub == "nav":
                try:
                    idx = int(parts[2])
                except (IndexError, ValueError):
                    idx = 0
                await q.answer()
                await _render_play_at(update, context, idx)

        # ----- game actions ----------------------------------------------- #
        elif namespace == "game":
            sub = parts[1] if len(parts) > 1 else ""
            slug = parts[2] if len(parts) > 2 else ""
            if sub == "view" and slug:
                await q.answer()
                await _render_game_detail(update, context, slug)
            elif sub == "track" and slug:
                bc.store.track(chat_id, slug)
                await q.answer("✅ Tracking", show_alert=False)
                await _render_game_detail(update, context, slug)
            elif sub == "untrack" and slug:
                bc.store.untrack(chat_id, slug)
                await q.answer("Stopped tracking", show_alert=False)
                await _render_tracked(update, context)

        else:
            await q.answer()

    except Exception as e:
        logger.exception("callback %s failed", data)
        try:
            await q.answer("⚠️ Error", show_alert=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Free-form text (used when we've prompted for bankroll input)
# --------------------------------------------------------------------------- #

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    awaiting = context.chat_data.get("awaiting")
    if not awaiting or not update.message or not update.message.text:
        return

    bc = _ctx(context)
    text = update.message.text.strip()

    if awaiting == "bankroll":
        try:
            v = float(text.replace(",", "").replace("$", ""))
        except ValueError:
            await update.message.reply_text(
                "That doesn't look like a number. Try again or /cancel."
            )
            return
        v = max(10.0, min(10_000_000.0, v))
        bc.store.set_pref(update.effective_chat.id, bankroll=v)
        context.chat_data.pop("awaiting", None)
        await update.message.reply_text(
            f"✅ Bankroll set to <b>${v:,.2f}</b>.",
            parse_mode=ParseMode.HTML,
        )
        await _render_settings(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    context.chat_data.pop("awaiting", None)
    await update.effective_chat.send_message("Cancelled.")
