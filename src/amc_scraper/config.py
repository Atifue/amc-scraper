from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _parse_post_time(raw: str, tz: ZoneInfo) -> time:
    hour_s, minute_s = raw.strip().split(":", 1)
    return time(hour=int(hour_s), minute=int(minute_s), tzinfo=tz)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_channel_id: int
    discord_guild_id: int | None
    post_time: time
    timezone: ZoneInfo
    amc_vendor_key: str | None
    cache_ttl_seconds: int = 300
    inter_theatre_delay: float = 0.75
    watch_theatre: str = "lincoln-square"
    watch_interval_seconds: int = 60
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )

    @classmethod
    def from_env(cls, *, require_discord: bool = True) -> Settings:
        load_dotenv()
        tz_name = os.getenv("TIMEZONE", "America/New_York")
        tz = ZoneInfo(tz_name)
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_discord and not token:
            raise RuntimeError("DISCORD_TOKEN is required")

        channel_raw = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        if require_discord and not channel_raw:
            raise RuntimeError("DISCORD_CHANNEL_ID is required")
        channel_id = int(channel_raw) if channel_raw else 0

        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        vendor_key = os.getenv("AMC_VENDOR_KEY", "").strip() or None
        ttl = int(os.getenv("AMC_CACHE_TTL_SECONDS", "300"))

        return cls(
            discord_token=token,
            discord_channel_id=channel_id,
            discord_guild_id=int(guild_raw) if guild_raw else None,
            post_time=_parse_post_time(os.getenv("POST_TIME", "09:00"), tz),
            timezone=tz,
            amc_vendor_key=vendor_key,
            cache_ttl_seconds=ttl,
            watch_theatre=os.getenv("WATCH_THEATRE", "lincoln-square").strip() or "lincoln-square",
            watch_interval_seconds=int(os.getenv("WATCH_INTERVAL_SECONDS", "60")),
        )
