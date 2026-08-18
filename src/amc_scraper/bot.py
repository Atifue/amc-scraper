from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dt_time

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks

from .client import AmcClient, ShowtimeError
from .config import Settings
from .formatter import (
    listing_to_embed_payloads,
    new_showtimes_to_embed_payloads,
    schedule_to_embed_payloads,
)
from .fandango import today_in
from .models import TheatreDay, TheatreSchedule
from .theatres import THEATRES, get_theatre
from .watch import load_seen, save_seen, showtimes_from_listings

log = logging.getLogger(__name__)

THEATER_CHOICES = [
    app_commands.Choice(name="All", value="all"),
    *[app_commands.Choice(name=theatre.name, value=theatre.key) for theatre in THEATRES],
]


def _embeds_from_payloads(payloads: list[dict]) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    for payload in payloads:
        embed = discord.Embed(
            title=payload["title"],
            description=payload["description"],
            color=payload["color"],
            url=payload["url"],
        )
        footer = payload.get("footer") or {}
        if footer.get("text"):
            embed.set_footer(text=footer["text"])
        embeds.append(embed)
    return embeds


def _embeds_from_listing(listing: TheatreDay) -> list[discord.Embed]:
    return _embeds_from_payloads(listing_to_embed_payloads(listing))


def _chunks(items: list[discord.Embed], size: int) -> list[list[discord.Embed]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


class ShowtimesBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.amc = AmcClient(settings)
        self._watch_busy = False
        self._watch_resume_at = 0.0
        self.daily_showtimes.change_interval(time=settings.post_time)
        self.watch_showtimes.change_interval(seconds=settings.watch_interval_seconds)

    async def setup_hook(self) -> None:
        self.tree.add_command(showtimes)
        self.tree.add_command(coming)
        guild_id = self.settings.discord_guild_id
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %s guild command(s) to %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %s global command(s)", len(synced))
        self.daily_showtimes.start()
        self.watch_showtimes.start()

    async def on_ready(self) -> None:
        user = self.user
        log.info("Logged in as %s (%s)", user, user.id if user else "?")

    @tasks.loop(time=dt_time(hour=9, minute=0))
    async def daily_showtimes(self) -> None:
        channel_id = self.settings.discord_channel_id
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        except discord.HTTPException:
            log.exception("Could not fetch DISCORD_CHANNEL_ID %s", channel_id)
            return
        if not isinstance(channel, discord.abc.Messageable):
            log.error("DISCORD_CHANNEL_ID %s is not a text channel", channel_id)
            return
        try:
            listings = await self.amc.fetch_many(list(THEATRES), remaining_only=True)
        except ShowtimeError:
            log.exception("Daily showtimes fetch failed")
            await channel.send("Could not load today's AMC showtimes. Try `/showtimes` later.")
            return
        for listing in listings:
            embeds = _embeds_from_listing(listing)
            for batch in _chunks(embeds, 10):
                await channel.send(embeds=batch)

    @daily_showtimes.before_loop
    async def before_daily_showtimes(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=60)
    async def watch_showtimes(self) -> None:
        if self._watch_busy:
            log.info("Skipping Lincoln Square watch; previous scan still running")
            return
        if time.monotonic() < self._watch_resume_at:
            return
        self._watch_busy = True
        try:
            await self._poll_lincoln_square()
        except Exception:
            log.exception("Lincoln Square watch tick failed")
        finally:
            self._watch_busy = False

    @watch_showtimes.before_loop
    async def before_watch_showtimes(self) -> None:
        await self.wait_until_ready()

    async def _poll_lincoln_square(self) -> None:
        theatre = get_theatre(self.settings.watch_theatre)
        try:
            listings = await self.amc.fetch_schedule_listings(theatre, use_cache=False)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 429}:
                log.warning(
                    "HTTP %s during %s watch; backing off 10 minutes",
                    exc.response.status_code,
                    theatre.name,
                )
                self._watch_resume_at = time.monotonic() + 600
                return
            log.exception("%s watch fetch failed", theatre.name)
            return
        except ShowtimeError as exc:
            message = str(exc)
            if "HTTP 403" in message or "HTTP 429" in message:
                log.warning(
                    "%s watch blocked by Fandango; backing off 10 minutes: %s",
                    theatre.name,
                    exc,
                )
                self._watch_resume_at = time.monotonic() + 600
                return
            log.exception("%s watch fetch failed", theatre.name)
            return

        current = showtimes_from_listings(listings, buyable_only=True)
        current_keys = {item.key() for item in current}
        initialized, seen = load_seen()
        if not initialized:
            save_seen(current_keys)
            log.info(
                "Saved %s buyable-showtime baseline (%s keys); no alert",
                theatre.name,
                len(current_keys),
            )
            return

        new_keys = current_keys - seen
        if not new_keys:
            return

        new_items = [item for item in current if item.key() in new_keys]
        log.info("Buyable showtimes at %s: %s", theatre.name, len(new_items))

        channel_id = self.settings.discord_channel_id
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        except discord.HTTPException:
            log.exception("Could not fetch DISCORD_CHANNEL_ID %s", channel_id)
            return
        if not isinstance(channel, discord.abc.Messageable):
            log.error("DISCORD_CHANNEL_ID %s is not a text channel", channel_id)
            return

        embeds = _embeds_from_payloads(new_showtimes_to_embed_payloads(theatre, new_items))
        mention = discord.AllowedMentions(everyone=True)
        for index, batch in enumerate(_chunks(embeds, 10)):
            content = (
                f"@here Tickets are buyable at {theatre.name}"
                if index == 0
                else None
            )
            await channel.send(content=content, embeds=batch, allowed_mentions=mention)
        save_seen(seen | current_keys)


@app_commands.command(
    name="showtimes",
    description="List movies playing at the configured AMC theaters",
)
@app_commands.describe(
    theater="Which theater to list (defaults to all)",
    date="Date as YYYY-MM-DD (defaults to today)",
)
@app_commands.choices(theater=THEATER_CHOICES)
async def showtimes(
    interaction: discord.Interaction,
    theater: app_commands.Choice[str] | None = None,
    date: str | None = None,
) -> None:
    await interaction.response.defer()
    bot = interaction.client
    if not isinstance(bot, ShowtimesBot):
        await interaction.followup.send("Bot is not ready.")
        return

    try:
        day = _parse_optional_date(date)
    except ValueError:
        await interaction.followup.send("Date must be YYYY-MM-DD, for example `2026-08-15`.")
        return

    theatre_key = theater.value if theater else "all"
    theatres = list(THEATRES) if theatre_key == "all" else [get_theatre(theatre_key)]
    try:
        listings = await bot.amc.fetch_many(theatres, day, remaining_only=True)
    except ShowtimeError as exc:
        log.exception("showtimes command failed")
        await interaction.followup.send(f"Could not load showtimes: {exc}")
        return

    for listing in listings:
        embeds = _embeds_from_listing(listing)
        for batch in _chunks(embeds, 10):
            await interaction.followup.send(embeds=batch)


@app_commands.command(
    name="coming",
    description="List unique movies scheduled as far ahead as listings go",
)
@app_commands.describe(
    theater="Which theater to list (defaults to all)",
    through="Optional last date YYYY-MM-DD (default: keep looking until listings end)",
)
@app_commands.choices(theater=THEATER_CHOICES)
async def coming(
    interaction: discord.Interaction,
    theater: app_commands.Choice[str] | None = None,
    through: str | None = None,
) -> None:
    await interaction.response.defer()
    bot = interaction.client
    if not isinstance(bot, ShowtimesBot):
        await interaction.followup.send("Bot is not ready.")
        return

    try:
        end = _parse_optional_date(through)
    except ValueError:
        await interaction.followup.send("Date must be YYYY-MM-DD, for example `2026-12-31`.")
        return

    theatre_key = theater.value if theater else "all"
    theatres = list(THEATRES) if theatre_key == "all" else [get_theatre(theatre_key)]
    start = today_in(theatres[0].timezone)
    try:
        schedules = await bot.amc.fetch_schedules(theatres, start, end)
    except ShowtimeError as exc:
        log.exception("coming command failed")
        await interaction.followup.send(f"Could not load upcoming movies: {exc}")
        return

    for schedule in schedules:
        embeds = _embeds_from_schedule(schedule)
        for batch in _chunks(embeds, 10):
            await interaction.followup.send(embeds=batch)


def _embeds_from_schedule(schedule: TheatreSchedule) -> list[discord.Embed]:
    return _embeds_from_payloads(schedule_to_embed_payloads(schedule))


def _parse_optional_date(raw: str | None) -> date | None:
    if not raw:
        return None
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env(require_discord=True)
    ShowtimesBot(settings).run(settings.discord_token)


if __name__ == "__main__":
    main()
