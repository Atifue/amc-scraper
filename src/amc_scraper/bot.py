from __future__ import annotations

import logging
from datetime import date, datetime, time as dt_time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .client import AmcClient, ShowtimeError
from .config import Settings
from .formatter import listing_to_embed_payloads, schedule_to_embed_payloads, seat_map_to_embed_payloads
from .fandango import today_in
from .models import TheatreDay, TheatreSchedule
from .seats import SeatLookupError
from .theatres import DAILY_THEATRES, THEATRES, THEATRES_BY_KEY, get_theatre

log = logging.getLogger(__name__)

THEATER_CHOICES = [
    app_commands.Choice(name="Fresh Meadows + Bay Terrace", value="all"),
    *[app_commands.Choice(name=theatre.name, value=theatre.key) for theatre in THEATRES],
]
SEAT_THEATER_CHOICES = [
    app_commands.Choice(name=theatre.name, value=theatre.key) for theatre in THEATRES
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
        self.daily_showtimes.change_interval(time=settings.post_time)

    async def setup_hook(self) -> None:
        self.tree.add_command(showtimes)
        self.tree.add_command(coming)
        self.tree.add_command(seats)
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

    async def on_ready(self) -> None:
        user = self.user
        log.info("Logged in as %s (%s)", user, user.id if user else "?")
        self.loop.create_task(self._prefetch_today(), name="prefetch-showtimes")

    async def _prefetch_today(self) -> None:
        for theatre in THEATRES:
            try:
                await self.amc.fetch(theatre, remaining_only=True)
            except Exception:
                log.warning("Prefetch failed for %s", theatre.key, exc_info=True)

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
            listings = await self.amc.fetch_many(list(DAILY_THEATRES), remaining_only=True)
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


@app_commands.command(
    name="showtimes",
    description="List movies playing at the configured AMC theaters",
)
@app_commands.describe(
    theater="Which theater to list (defaults to Fresh Meadows and Bay Terrace)",
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
    theatres = list(DAILY_THEATRES) if theatre_key == "all" else [get_theatre(theatre_key)]
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
    theater="Which theater to list (defaults to Fresh Meadows and Bay Terrace)",
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
    theatres = list(DAILY_THEATRES) if theatre_key == "all" else [get_theatre(theatre_key)]
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


@app_commands.command(
    name="seats",
    description="Show a read-only Fandango seat map for an on-sale showtime",
)
@app_commands.describe(
    theater="Theater (required)",
    movie="Movie title, or enough of it to match",
    time="Showtime like 7:30 PM",
    date="Date as YYYY-MM-DD (defaults to today)",
    format="Optional format if two screens share the time, for example IMAX",
)
@app_commands.choices(theater=SEAT_THEATER_CHOICES)
async def seats(
    interaction: discord.Interaction,
    theater: app_commands.Choice[str],
    movie: str,
    time: str,
    date: str | None = None,
    format: str | None = None,
) -> None:
    await interaction.response.defer()
    bot = interaction.client
    if not isinstance(bot, ShowtimesBot):
        await interaction.followup.send("Bot is not ready.")
        return
    try:
        day = _parse_optional_date(date)
    except ValueError:
        await interaction.followup.send("Date must be YYYY-MM-DD, for example `2026-08-20`.")
        return
    try:
        movie_listing, show, seat_map = await bot.amc.fetch_seat_map(
            theater.value, movie, time, day, format
        )
    except SeatLookupError as exc:
        await interaction.followup.send(str(exc))
        return
    except ShowtimeError as exc:
        log.exception("seats command failed")
        await interaction.followup.send(f"Could not load the seat map: {exc}")
        return
    except Exception:
        log.exception("seats command failed")
        await interaction.followup.send("Could not load the seat map.")
        return

    theatre = get_theatre(theater.value)
    embeds = _embeds_from_payloads(
        seat_map_to_embed_payloads(theatre, movie_listing, show, seat_map)
    )
    for batch in _chunks(embeds, 10):
        await interaction.followup.send(embeds=batch)


@seats.autocomplete("movie")
async def seats_movie_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        listing = _seats_listing_for_autocomplete(interaction)
        if listing is None:
            return []
        needle = current.casefold()
        titles = [movie.title for movie in listing.movies if movie.showtimes]
        if needle:
            titles = [title for title in titles if needle in title.casefold()]
        return [
            app_commands.Choice(name=title[:100], value=title[:100])
            for title in titles[:25]
        ]
    except Exception:
        log.exception("seats movie autocomplete failed")
        return []


@seats.autocomplete("time")
async def seats_time_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        listing = _seats_listing_for_autocomplete(interaction)
        movie_query = _namespace_str(getattr(interaction.namespace, "movie", None))
        if listing is None or not movie_query:
            return []
        needle = current.casefold().replace(" ", "")
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()
        for movie in listing.movies:
            if movie_query.casefold() not in movie.title.casefold():
                continue
            for show in movie.showtimes:
                if not show.buyable:
                    continue
                stamp = _format_choice_clock(show.time_local)
                label = (
                    stamp
                    if show.format_name in {"", "Standard"}
                    else f"{stamp} · {show.format_name}"
                )[:100]
                if label in seen:
                    continue
                if needle and needle not in label.casefold().replace(" ", ""):
                    continue
                seen.add(label)
                choices.append(app_commands.Choice(name=label, value=stamp[:100]))
                if len(choices) >= 25:
                    return choices
        return choices
    except Exception:
        log.exception("seats time autocomplete failed")
        return []


def _seats_listing_for_autocomplete(interaction: discord.Interaction):
    bot = interaction.client
    if not isinstance(bot, ShowtimesBot):
        return None
    theatre_key = _namespace_theatre_key(getattr(interaction.namespace, "theater", None))
    if not theatre_key:
        return None
    date_raw = _namespace_str(getattr(interaction.namespace, "date", None))
    try:
        day = _parse_optional_date(date_raw) if date_raw else None
    except ValueError:
        day = None
    return bot.amc.cached_listing(theatre_key, day, remaining_only=True)


def _namespace_theatre_key(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, app_commands.Choice):
        raw = raw.value
    text = str(raw).strip()
    if text in THEATRES_BY_KEY:
        return text
    for theatre in THEATRES:
        if theatre.name == text:
            return theatre.key
    return None


def _namespace_str(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, app_commands.Choice):
        raw = raw.value
    text = str(raw).strip()
    return text or None


def _format_choice_clock(value: datetime) -> str:
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}"


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
