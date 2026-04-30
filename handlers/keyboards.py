"""Inline keyboard builders.

Callback data uses a colon-separated namespace so we can route in one place:

    menu:main          → main menu
    menu:games         → games list
    menu:value         → value plays
    menu:tracked       → tracked list
    menu:settings      → settings panel
    menu:help          → help screen

    set:bankroll       → prompt for bankroll
    set:edge:<pp>      → set edge threshold to <pp>
    set:conf:<n>       → set confidence threshold to <n>
    set:alerts:<on|off>→ toggle alerts

    game:view:<slug>   → show one game
    game:track:<slug>  → start tracking
    game:untrack:<slug>→ stop tracking

    play:next:<idx>    → cycle to next play in cached list
    play:prev:<idx>    → cycle back

Bots have a 64-byte limit on callback_data, so slugs sometimes get truncated.
The slug we send is always one Polymarket already gave us so it's stable.
"""
from __future__ import annotations

from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# --------------------------------------------------------------------------- #
# Top-level menus
# --------------------------------------------------------------------------- #

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚾ Today's Games", callback_data="menu:games"),
                InlineKeyboardButton("🎯 Value Plays", callback_data="menu:value"),
            ],
            [
                InlineKeyboardButton("📋 Tracked", callback_data="menu:tracked"),
                InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
            ],
            [
                InlineKeyboardButton("📖 How it works", callback_data="menu:help"),
            ],
        ]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("← Main menu", callback_data="menu:main")]]
    )


# --------------------------------------------------------------------------- #
# Games list
# --------------------------------------------------------------------------- #

def games_list_keyboard(slugs: list, *, refresh: bool = True) -> InlineKeyboardMarkup:
    rows: list = []
    # Two columns of game-detail buttons. Slug truncated for display, full slug
    # passed in callback_data — but callback_data is capped at 64 bytes so we
    # rely on slug brevity. Polymarket slugs are typically 30-50 chars.
    for i, slug in enumerate(slugs[:10]):
        label = f"View #{i + 1}"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"game:view:{slug}"[:60]),
            InlineKeyboardButton("👁 Track", callback_data=f"game:track:{slug}"[:60]),
        ])
    actions = []
    if refresh:
        actions.append(InlineKeyboardButton("🔄 Refresh", callback_data="menu:games"))
    actions.append(InlineKeyboardButton("← Main menu", callback_data="menu:main"))
    rows.append(actions)
    return InlineKeyboardMarkup(rows)


def game_detail_keyboard(slug: str, *, is_tracked: bool = False) -> InlineKeyboardMarkup:
    track_btn = (
        InlineKeyboardButton("🚫 Untrack", callback_data=f"game:untrack:{slug}"[:60])
        if is_tracked
        else InlineKeyboardButton("👁 Track", callback_data=f"game:track:{slug}"[:60])
    )
    return InlineKeyboardMarkup(
        [
            [track_btn],
            [
                InlineKeyboardButton("← Games", callback_data="menu:games"),
                InlineKeyboardButton("⌂ Menu", callback_data="menu:main"),
            ],
        ]
    )


# --------------------------------------------------------------------------- #
# Value plays — pagination
# --------------------------------------------------------------------------- #

def value_pager_keyboard(
    *, index: int, total: int, slug: Optional[str] = None
) -> InlineKeyboardMarkup:
    rows = []
    nav_row = []
    if total > 1:
        prev_idx = (index - 1) % total
        next_idx = (index + 1) % total
        nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"play:nav:{prev_idx}"))
        nav_row.append(InlineKeyboardButton(f"{index + 1}/{total}", callback_data="play:noop"))
        nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"play:nav:{next_idx}"))
        rows.append(nav_row)
    if slug:
        rows.append([
            InlineKeyboardButton("👁 Track this game", callback_data=f"game:track:{slug}"[:60]),
        ])
    rows.append([
        InlineKeyboardButton("🔄 Re-scan", callback_data="menu:value"),
        InlineKeyboardButton("⌂ Menu", callback_data="menu:main"),
    ])
    return InlineKeyboardMarkup(rows)


def empty_value_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚙️ Loosen filters", callback_data="menu:settings"),
                InlineKeyboardButton("🔄 Re-scan", callback_data="menu:value"),
            ],
            [InlineKeyboardButton("⌂ Main menu", callback_data="menu:main")],
        ]
    )


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def settings_keyboard(*, alerts_on: bool) -> InlineKeyboardMarkup:
    alerts_label = "🔕 Turn alerts OFF" if alerts_on else "🔔 Turn alerts ON"
    alerts_cb = f"set:alerts:{'off' if alerts_on else 'on'}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💵 Bankroll", callback_data="set:bankroll:prompt"),
            ],
            [
                InlineKeyboardButton("Edge: 3pp", callback_data="set:edge:3.0"),
                InlineKeyboardButton("4pp", callback_data="set:edge:4.0"),
                InlineKeyboardButton("5pp", callback_data="set:edge:5.0"),
                InlineKeyboardButton("7pp", callback_data="set:edge:7.0"),
            ],
            [
                InlineKeyboardButton("Conf: 50", callback_data="set:conf:50"),
                InlineKeyboardButton("60", callback_data="set:conf:60"),
                InlineKeyboardButton("70", callback_data="set:conf:70"),
                InlineKeyboardButton("80", callback_data="set:conf:80"),
            ],
            [InlineKeyboardButton(alerts_label, callback_data=alerts_cb)],
            [InlineKeyboardButton("← Main menu", callback_data="menu:main")],
        ]
    )


# --------------------------------------------------------------------------- #
# Tracked list
# --------------------------------------------------------------------------- #

def tracked_keyboard(slugs: list) -> InlineKeyboardMarkup:
    rows = []
    for slug in slugs[:15]:
        rows.append([
            InlineKeyboardButton(
                f"🚫 Untrack {slug[:25]}",
                callback_data=f"game:untrack:{slug}"[:60],
            )
        ])
    rows.append([
        InlineKeyboardButton("⚾ Browse games", callback_data="menu:games"),
        InlineKeyboardButton("⌂ Menu", callback_data="menu:main"),
    ])
    return InlineKeyboardMarkup(rows)
