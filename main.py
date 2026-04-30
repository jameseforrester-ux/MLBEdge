"""Application entry point.

Wires up the Polymarket client, storage, HTTP client, command/callback
handlers, and the JobQueue. Then runs in long-poll mode.
"""
from __future__ import annotations

import logging
import sys

import httpx
from telegram import BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import load_settings
from handlers import commands, jobs
from polymarket.client import PolymarketClient
from storage import Store


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Telegram's lib is chatty at INFO — bump it down a notch
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)


async def _post_init(app: Application) -> None:
    """Set the visible command list shown in Telegram's `/` menu."""
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Show the main menu"),
            BotCommand("games", "Today's MLB markets"),
            BotCommand("value", "Top value plays right now"),
            BotCommand("tracked", "Games you're tracking"),
            BotCommand("settings", "Edit your thresholds & bankroll"),
            BotCommand("track", "Track a game by slug"),
            BotCommand("untrack", "Stop tracking a game"),
            BotCommand("alerts", "Toggle background alerts on/off"),
            BotCommand("bankroll", "Set your bankroll"),
            BotCommand("edge", "Set min edge in pp"),
            BotCommand("conf", "Set min confidence (0-100)"),
            BotCommand("help", "How the bot works"),
        ]
    )


async def _post_shutdown(app: Application) -> None:
    bc = app.bot_data.get("ctx")
    if bc:
        try:
            await bc.poly.aclose()
        except Exception:
            pass
        try:
            await bc.http.aclose()
        except Exception:
            pass


def build_app() -> Application:
    settings = load_settings()
    _configure_logging(settings.log_level)

    poly = PolymarketClient(
        gamma_url=settings.gamma_url,
        clob_url=settings.clob_url,
    )
    http = httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": "polymarket-mlb-bot/1.0"},
    )
    store = Store("bot.db")

    bot_ctx = commands.BotContext(
        settings=settings, poly=poly, store=store, http=http
    )

    app = (
        ApplicationBuilder()
        .token(settings.telegram_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["ctx"] = bot_ctx

    # Slash commands
    app.add_handler(CommandHandler("start", commands.cmd_start))
    app.add_handler(CommandHandler("help", commands.cmd_help))
    app.add_handler(CommandHandler("games", commands.cmd_games))
    app.add_handler(CommandHandler("value", commands.cmd_value))
    app.add_handler(CommandHandler("settings", commands.cmd_settings))
    app.add_handler(CommandHandler("tracked", commands.cmd_tracked))
    app.add_handler(CommandHandler("track", commands.cmd_track))
    app.add_handler(CommandHandler("untrack", commands.cmd_untrack))
    app.add_handler(CommandHandler("alerts", commands.cmd_alerts))
    app.add_handler(CommandHandler("edge", commands.cmd_edge))
    app.add_handler(CommandHandler("conf", commands.cmd_conf))
    app.add_handler(CommandHandler("bankroll", commands.cmd_bankroll))
    app.add_handler(CommandHandler("cancel", commands.cmd_cancel))

    # Inline button callbacks + free-form text (for bankroll prompt)
    app.add_handler(CallbackQueryHandler(commands.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, commands.on_text))

    # Background jobs
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            jobs.tracked_price_job,
            interval=settings.track_refresh_seconds,
            first=30,
            name="tracked_price_job",
        )
        app.job_queue.run_repeating(
            jobs.value_scan_job,
            interval=settings.value_scan_seconds,
            first=60,
            name="value_scan_job",
        )
    else:
        logging.getLogger(__name__).warning(
            "JobQueue not available — install python-telegram-bot[job-queue] "
            "to enable background alerts."
        )

    return app


def main() -> int:
    try:
        app = build_app()
    except RuntimeError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    app.run_polling(allowed_updates=["message", "callback_query"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
