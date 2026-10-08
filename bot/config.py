"""Configuration loaded from environment variables (never hard-code secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

MARKET_TZ = ZoneInfo("America/New_York")  # futures sessions, calendar and market posts run on New York time


def _int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw.lstrip("#"), 16) if raw.lower().startswith(("0x", "#")) else int(raw)


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


@dataclass(frozen=True)
class Config:
    token: str
    owner_id: int
    guild_id: int | None
    bot_name: str
    brand_color: int
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_base_url: str
    vision_provider: str
    vision_api_key: str
    vision_model: str
    giphy_api_key: str
    tz: ZoneInfo
    db_path: str
    report_hour: int
    status_hour: int
    auto_rebuild: bool
    log_dir: str
    active_start_hour: int
    active_end_hour: int
    quiet_minutes: int
    max_starters_per_day: int

    @classmethod
    def from_env(cls, require_token: bool = True) -> "Config":
        token = _str("DISCORD_TOKEN")
        if require_token and not token:
            raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        owner = _int("OWNER_ID", 0)
        if require_token and not owner:
            raise SystemExit("OWNER_ID is not set. The bot needs to know who you are.")
        llm_provider = _str("LLM_PROVIDER", "none").lower() or "none"
        llm_key = _str("LLM_API_KEY")
        # No separate vision key: read screenshots with the text AI's provider and key (Groq has a free vision model).
        vision_key = _str("VISION_API_KEY")
        vision_provider = _str("VISION_PROVIDER", llm_provider).lower() if vision_key else llm_provider
        return cls(
            token=token,
            owner_id=owner or 0,
            guild_id=_int("GUILD_ID"),
            bot_name=_str("BOT_NAME", "Sofie"),
            brand_color=_int("BRAND_COLOR", 0x00C2A8),
            llm_provider=llm_provider,
            llm_api_key=llm_key,
            llm_model=_str("LLM_MODEL"),
            llm_base_url=_str("LLM_BASE_URL"),
            # Screenshot reading. Gemini's free tier reads trading screenshots well.
            vision_provider=vision_provider,
            vision_api_key=vision_key or llm_key,
            vision_model=_str("VISION_MODEL"),
            giphy_api_key=_str("GIPHY_API_KEY"),
            tz=ZoneInfo(_str("TIMEZONE", "America/New_York") or "America/New_York"),
            db_path=_str("DB_PATH", "data/bot.db"),
            report_hour=_int("REPORT_HOUR", 18),
            status_hour=_int("STATUS_HOUR", 8),
            auto_rebuild=_str("AUTO_REBUILD", "on").lower() not in ("off", "0", "false", "no"),
            log_dir=_str("LOG_DIR", "data/logs"),
            active_start_hour=_int("ACTIVE_START_HOUR", 8),
            active_end_hour=_int("ACTIVE_END_HOUR", 23),
            quiet_minutes=_int("QUIET_MINUTES", 120),
            max_starters_per_day=_int("MAX_STARTERS_PER_DAY", 3),
        )
