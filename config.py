"""Central configuration. All env vars are loaded here and nowhere else."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Set

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _id_set(name: str) -> Set[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    out: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            pass
    return out


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_user_ids: Set[int]

    gamma_url: str
    clob_url: str

    default_min_edge_pct: float
    default_min_confidence: int
    min_liquidity_usdc: float

    track_refresh_seconds: int
    value_scan_seconds: int

    log_level: str


def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    return Settings(
        telegram_token=token,
        allowed_user_ids=_id_set("ALLOWED_USER_IDS"),
        gamma_url=os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com").rstrip("/"),
        clob_url=os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com").rstrip("/"),
        default_min_edge_pct=_float("DEFAULT_MIN_EDGE_PCT", 4.0),
        default_min_confidence=_int("DEFAULT_MIN_CONFIDENCE", 55),
        min_liquidity_usdc=_float("MIN_LIQUIDITY_USDC", 2000.0),
        track_refresh_seconds=_int("TRACK_REFRESH_SECONDS", 120),
        value_scan_seconds=_int("VALUE_SCAN_SECONDS", 600),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
