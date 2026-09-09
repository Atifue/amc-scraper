"""Reproduce and verify the fix for issue #1 (/seats dropdown goes blank).

Hits live Fandango data:

    ./.venv/bin/python scripts/check_seats.py
"""

from __future__ import annotations

import asyncio
import time as clock
from types import SimpleNamespace

from amc_scraper import bot as botmod
from amc_scraper.client import AmcClient
from amc_scraper.config import Settings
from amc_scraper.seats import parse_clock_candidates
from amc_scraper.theatres import FRESH_MEADOWS, THEATRES

THEATRE = FRESH_MEADOWS


class FakeInteraction:
    """Stand-in for discord.Interaction inside an autocomplete callback."""

    def __init__(self, client, **options) -> None:
        self.client = client
        self.namespace = SimpleNamespace(**options)


def go_cold(client: AmcClient) -> None:
    """Simulate AMC_CACHE_TTL_SECONDS elapsing, which is what broke /seats."""
    client._cache = {
        key: (clock.monotonic() - 1, listing) for key, listing in client._cache.items()
    }


async def timed(label: str, coro):
    started = clock.perf_counter()
    result = await coro
    elapsed = (clock.perf_counter() - started) * 1000
    names = [choice.name for choice in result]
    print(f"  {label}: {len(names)} options in {elapsed:.0f} ms -> {names[:5]}")
    return result


async def check_client() -> None:
    print("client cache")
    client = AmcClient(Settings.from_env(require_discord=False))
    listing = await client.fetch(THEATRE, remaining_only=True)
    on_sale = [
        (movie.title, show)
        for movie in listing.movies
        for show in movie.showtimes
        if show.buyable
    ]
    print(f"  warm: {len(listing.movies)} movies, {len(on_sale)} on-sale showtimes")

    go_cold(client)
    stale = client.cached_listing(THEATRE, allow_stale=False)
    print(f"  before the fix: {'blank dropdown' if stale is None else 'ok'}")

    started = clock.perf_counter()
    fixed = await client.listing_for_autocomplete(THEATRE)
    elapsed = clock.perf_counter() - started
    assert fixed and fixed.movies, "autocomplete still blank after the TTL expired"
    assert elapsed < 0.05, "autocomplete blocked on the network"
    print(f"  after the fix: {len(fixed.movies)} movies in {elapsed * 1000:.0f} ms")

    go_cold(client)
    answered = await asyncio.gather(
        *[client.listing_for_autocomplete(THEATRE) for _ in range(8)]
    )
    assert len(client._inflight) <= 1, "one keystroke burst fanned out into many fetches"
    print(f"  8 concurrent keystrokes: {sum(x is not None for x in answered)} answered, "
          f"{len(client._inflight)} shared refresh in flight")

    if on_sale:
        title, show = on_sale[0]
        typed = f"{show.time_local.hour % 12 or 12}{show.time_local.minute:02d}"
        movie, matched, seat_map = await client.fetch_seat_map(THEATRE, title, typed)
        print(f"  hand-typed time {typed!r} -> "
              f"{[str(c) for c in parse_clock_candidates(typed)]} -> "
              f"{movie.title} {matched.time_local:%I:%M %p}, "
              f"{seat_map.available}/{seat_map.total} open")

    await asyncio.gather(*client._inflight.values(), return_exceptions=True)


async def check_dropdowns() -> None:
    print("discord autocomplete callbacks")
    settings = Settings.from_env(require_discord=False)
    bot = botmod.ShowtimesBot.__new__(botmod.ShowtimesBot)
    botmod.commands.Bot.__init__(
        bot, command_prefix="!", intents=botmod.discord.Intents.default()
    )
    bot.settings = settings
    bot.amc = AmcClient(settings)

    await bot.amc.fetch(THEATRE, remaining_only=True)
    go_cold(bot.amc)

    movies = await timed(
        "movie, theater picked",
        botmod.seats_movie_autocomplete(
            FakeInteraction(bot, theater=THEATRE.key, date=None), ""
        ),
    )
    assert movies, "movie dropdown blank with a theater picked"

    await timed(
        "movie, nothing picked yet",
        botmod.seats_movie_autocomplete(FakeInteraction(bot), ""),
    )
    await timed(
        f"movie, typing {movies[0].value[:4]!r}",
        botmod.seats_movie_autocomplete(
            FakeInteraction(bot, theater=THEATRE.key), movies[0].value[:4]
        ),
    )

    times = await timed(
        "time, movie picked",
        botmod.seats_time_autocomplete(
            FakeInteraction(bot, theater=THEATRE.key, movie=movies[0].value), ""
        ),
    )
    assert times, "time dropdown blank with a movie picked"

    await timed(
        "time, unparseable date option",
        botmod.seats_time_autocomplete(
            FakeInteraction(
                bot, theater=THEATRE.key, movie=movies[0].value, date="nonsense"
            ),
            "",
        ),
    )

    assert hasattr(bot, "refresh_listings"), "keepalive loop missing"
    print(f"  keepalive: every {botmod.AUTOCOMPLETE_REFRESH_MINUTES} min "
          f"across {len(THEATRES)} theaters")

    await asyncio.gather(*bot.amc._inflight.values(), return_exceptions=True)
    await bot.close()


async def main() -> None:
    await check_client()
    await check_dropdowns()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
