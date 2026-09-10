from __future__ import annotations

import asyncio
import io
import logging
from datetime import date, datetime, time as dt_time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .client import AmcClient, ShowtimeError
from .config import Settings
from .formatter import listing_to_embed_payloads, schedule_to_embed_payloads, seat_map_to_embed_payloads
from .fandango import today_in
from .models import MovieListing, TheatreDay, TheatreSchedule
from .seats import SeatLookupError, matching_movies, render_seat_map_png
from .theatres import DAILY_THEATRES, THEATRES, THEATRES_BY_KEY, get_theatre

log = logging.getLogger(__name__)

THEATER_CHOICES = [
    app_commands.Choice(name="Fresh Meadows + Bay Terrace", value="all"),
    *[app_commands.Choice(name=theatre.name, value=theatre.key) for theatre in THEATRES],
]
SEAT_THEATER_CHOICES = [
    app_commands.Choice(name=theatre.name, value=theatre.key) for theatre in THEATRES
]
# Keepalive for the autocomplete snapshot. Slow enough that Fandango does not
# start answering 403 from the VM.
AUTOCOMPLETE_REFRESH_MINUTES = 10
# Discord drops an autocomplete response after 3 seconds.
AUTOCOMPLETE_BUDGET_SECONDS = 2.0
# The upcoming window is a multi-day scan, so refresh it far less often.
UPCOMING_REFRESH_HOURS = 6


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
        image = payload.get("image") or {}
        if image.get("url"):
            embed.set_image(url=image["url"])
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
        self.refresh_listings.start()
        self.refresh_upcoming.start()
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
        for index, theatre in enumerate(THEATRES):
            if index:
                await asyncio.sleep(self.settings.inter_theatre_delay)
            try:
                await self.amc.fetch(theatre, remaining_only=True)
            except Exception:
                log.warning("Prefetch failed for %s", theatre.key, exc_info=True)
        await self._refresh_upcoming()

    async def _refresh_upcoming(self) -> None:
        for index, theatre in enumerate(THEATRES):
            if index:
                await asyncio.sleep(self.settings.inter_theatre_delay)
            try:
                days = await self.amc.refresh_upcoming(theatre)
                log.info("Warmed %s upcoming day(s) for %s", len(days), theatre.key)
            except Exception as exc:
                log.warning("Upcoming refresh failed for %s: %s", theatre.key, exc)

    @tasks.loop(minutes=AUTOCOMPLETE_REFRESH_MINUTES)
    async def refresh_listings(self) -> None:
        """Keep today's listings warm so /seats autocomplete is never empty.

        The listing cache TTL is only a few minutes, and autocomplete cannot
        afford a cold fetch inside Discord's 3 second window.
        """
        for index, theatre in enumerate(THEATRES):
            if index:
                await asyncio.sleep(self.settings.inter_theatre_delay)
            try:
                await self.amc.fetch(theatre, remaining_only=True)
            except Exception as exc:
                log.warning("Listing refresh failed for %s: %s", theatre.key, exc)

    @refresh_listings.before_loop
    async def before_refresh_listings(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(hours=UPCOMING_REFRESH_HOURS)
    async def refresh_upcoming(self) -> None:
        """Keep the upcoming window warm so /seats can suggest future movies."""
        await self._refresh_upcoming()

    @refresh_upcoming.before_loop
    async def before_refresh_upcoming(self) -> None:
        await self.wait_until_ready()

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
    movie="Movie title — same list as /coming",
    date="Date that movie plays, as YYYY-MM-DD",
    time="Showtime like 7:30 PM",
    format="Optional format if two screens share the time, for example IMAX",
)
@app_commands.choices(theater=SEAT_THEATER_CHOICES)
async def seats(
    interaction: discord.Interaction,
    theater: app_commands.Choice[str],
    movie: str,
    date: str,
    time: str,
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
    if day is None:
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
    png = render_seat_map_png(seat_map)
    image = discord.File(io.BytesIO(png), filename="seats.png")
    await interaction.followup.send(embed=embeds[0], file=image)


@seats.autocomplete("movie")
async def seats_movie_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        bot = interaction.client
        if not isinstance(bot, ShowtimesBot):
            return []
        theatres = _seats_theatres(interaction)
        needle = current.casefold()
        first_day: dict[str, date] = {}
        for theatre in theatres:
            for movie in bot.amc.coming_movies(theatre):
                if needle and needle not in movie.title.casefold():
                    continue
                previous = first_day.get(movie.title)
                if previous is None or movie.first_date < previous:
                    first_day[movie.title] = movie.first_date
        ordered = sorted(first_day.items(), key=lambda item: (item[1], item[0].casefold()))
        return [
            app_commands.Choice(name=title[:100], value=title[:100])
            for title, _day in ordered[:25]
        ]
    except Exception:
        log.exception("seats movie autocomplete failed")
        return []


@seats.autocomplete("date")
async def seats_date_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        listings = await _seats_listings_for_autocomplete(interaction)
        movie_query = _namespace_str(getattr(interaction.namespace, "movie", None))
        needle = current.casefold().replace(" ", "")
        today = today_in(THEATRES[0].timezone)
        days: list[date] = []
        seen: set[date] = set()
        for listing in listings:
            movies = (
                _autocomplete_movies(listing, movie_query)
                if movie_query
                else listing.movies
            )
            if not any(movie.showtimes for movie in movies):
                continue
            if listing.date in seen:
                continue
            label = _date_choice_label(listing.date, today)
            if needle and needle not in label.casefold().replace(" ", "") and needle not in listing.date.isoformat():
                continue
            seen.add(listing.date)
            days.append(listing.date)
            if len(days) >= 25:
                break
        return [
            app_commands.Choice(name=_date_choice_label(day, today), value=day.isoformat())
            for day in days
        ]
    except Exception:
        log.exception("seats date autocomplete failed")
        return []


@seats.autocomplete("time")
async def seats_time_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        date_raw = _namespace_str(getattr(interaction.namespace, "date", None))
        try:
            day = _parse_optional_date(date_raw) if date_raw else None
        except ValueError:
            day = None
        if day is None:
            return []
        listings = await _seats_listings_for_autocomplete(interaction)
        movie_query = _namespace_str(getattr(interaction.namespace, "movie", None))
        needle = current.casefold().replace(" ", "")
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()
        for listing in listings:
            if listing.date != day:
                continue
            movies = (
                _autocomplete_movies(listing, movie_query) if movie_query else listing.movies
            )
            for movie in movies:
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


def _date_choice_label(day: date, today: date) -> str:
    if day == today:
        return f"Today · {day.isoformat()}"[:100]
    return f"{day:%a %b %-d} · {day.isoformat()}"[:100]


def _autocomplete_movies(listing: TheatreDay, query: str) -> list[MovieListing]:
    matches = matching_movies(listing.movies, query)
    if matches:
        return matches
    needle = query.casefold()
    return [movie for movie in listing.movies if needle in movie.title.casefold()]


def _seats_theatres(interaction: discord.Interaction):
    theatre_key = _namespace_theatre_key(getattr(interaction.namespace, "theater", None))
    if theatre_key:
        return [get_theatre(theatre_key)]
    return list(THEATRES)


async def _seats_listings_for_autocomplete(
    interaction: discord.Interaction,
) -> list[TheatreDay]:
    """Days to build /seats date and time suggestions from.

    Movies come from the /coming calendar. Dates and times come from the
    warmed day listings for the chosen theater (or every theater if none
    is picked yet). An explicit date pins time suggestions to that day.
    """
    bot = interaction.client
    if not isinstance(bot, ShowtimesBot):
        return []
    date_raw = _namespace_str(getattr(interaction.namespace, "date", None))
    try:
        day = _parse_optional_date(date_raw) if date_raw else None
    except ValueError:
        day = None

    listings: list[TheatreDay] = []
    for theatre in _seats_theatres(interaction):
        if day is not None:
            pinned = await bot.amc.listing_for_autocomplete(
                theatre, day, remaining_only=True, timeout=AUTOCOMPLETE_BUDGET_SECONDS
            )
            if pinned:
                listings.append(pinned)
            continue
        upcoming = bot.amc.upcoming_listings(theatre, remaining_only=False)
        if upcoming:
            listings.extend(upcoming)
            continue
        today_listing = await bot.amc.listing_for_autocomplete(
            theatre, None, remaining_only=False, timeout=AUTOCOMPLETE_BUDGET_SECONDS
        )
        if today_listing:
            listings.append(today_listing)
    listings.sort(key=lambda item: item.date)
    return listings


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
