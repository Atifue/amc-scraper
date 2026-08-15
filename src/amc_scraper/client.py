from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

import httpx

from . import amc_api, amc_web
from .config import Settings
from .fandango import USER_AGENT, parse_fandango_payload, today_in
from .models import MovieListing, Theatre, TheatreDay
from .theatres import THEATRES, get_theatre

log = logging.getLogger(__name__)


class ShowtimeError(RuntimeError):
    pass


class AmcClient:
    """Fetch normalized showtimes, preferring the official API when a vendor key is set.

    Without a key, AMC's public website is behind Queue-it/Cloudflare, so the
    working HTTP path is Fandango's public theater JSON for the same AMC locations.
    Playwright against amctheatres.com is a last-resort fallback.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env(require_discord=False)
        self._cache: dict[tuple[str, str], tuple[float, TheatreDay]] = {}
        self._lock = asyncio.Lock()

    async def fetch(
        self,
        theatre: Theatre | str,
        day: date | None = None,
        *,
        remaining_only: bool = True,
    ) -> TheatreDay:
        if isinstance(theatre, str):
            theatre = get_theatre(theatre)
        day = day or today_in(theatre.timezone)
        async with self._lock:
            cached = self._get_cached(theatre, day)
            if cached is not None:
                return self._filter(cached, remaining_only)
            listing = await self._fetch_uncached(theatre, day)
            self._store_cached(theatre, day, listing)
            return self._filter(listing, remaining_only)

    async def fetch_many(
        self,
        theatres: list[Theatre] | None = None,
        day: date | None = None,
        *,
        remaining_only: bool = True,
    ) -> list[TheatreDay]:
        theatres = theatres or list(THEATRES)
        listings: list[TheatreDay] = []
        for index, theatre in enumerate(theatres):
            if index:
                await asyncio.sleep(self.settings.inter_theatre_delay)
            listings.append(
                await self.fetch(theatre, day, remaining_only=remaining_only)
            )
        return listings

    async def _fetch_uncached(self, theatre: Theatre, day: date) -> TheatreDay:
        errors: list[str] = []
        if self.settings.amc_vendor_key:
            try:
                async with self._http() as client:
                    return await amc_api.fetch_theatre_day(
                        client, theatre, day, self.settings.amc_vendor_key
                    )
            except Exception as exc:
                log.warning("AMC API failed for %s: %s", theatre.key, exc)
                errors.append(f"amc-api: {exc}")

        try:
            return await self._fetch_fandango(theatre, day)
        except Exception as exc:
            log.warning("Fandango listings failed for %s: %s", theatre.key, exc)
            errors.append(f"fandango: {exc}")

        try:
            return await amc_web.fetch_theatre_day(
                theatre, day, self.settings.user_agent
            )
        except Exception as exc:
            log.warning("AMC website scrape failed for %s: %s", theatre.key, exc)
            errors.append(f"amc-web: {exc}")

        raise ShowtimeError(
            f"Could not load showtimes for {theatre.name}: " + " | ".join(errors)
        )

    async def _fetch_fandango(self, theatre: Theatre, day: date) -> TheatreDay:
        url = (
            "https://www.fandango.com/napi/theaterMovieShowtimes/"
            f"{theatre.fandango_id}?startDate={day.isoformat()}&isdesktop=true"
        )
        page_slug = theatre.fandango_slug or theatre.path.split("/")[-1]
        referer = f"https://www.fandango.com/{page_slug}-{theatre.fandango_id}/theater-page"

        async with self._http() as client:
            response = await client.get(
                url,
                headers={
                    "Accept": "application/json",
                    "Referer": referer,
                    "Origin": "https://www.fandango.com",
                },
            )
            response.raise_for_status()
            payload = response.json()
        listing = parse_fandango_payload(theatre, day, payload)
        if not listing.movies:
            raise ShowtimeError(f"Fandango returned no movies for {theatre.name} on {day}")
        return listing

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": self.settings.user_agent or USER_AGENT},
            timeout=httpx.Timeout(25.0),
            follow_redirects=True,
        )

    def _get_cached(self, theatre: Theatre, day: date) -> TheatreDay | None:
        item = self._cache.get((theatre.key, day.isoformat()))
        if item is None:
            return None
        expires_at, listing = item
        if time.monotonic() > expires_at:
            self._cache.pop((theatre.key, day.isoformat()), None)
            return None
        return listing

    def _store_cached(self, theatre: Theatre, day: date, listing: TheatreDay) -> None:
        ttl = max(0, self.settings.cache_ttl_seconds)
        self._cache[(theatre.key, day.isoformat())] = (
            time.monotonic() + ttl,
            listing,
        )

    @staticmethod
    def _filter(listing: TheatreDay, remaining_only: bool) -> TheatreDay:
        if not remaining_only:
            return listing
        movies = []
        for movie in listing.movies:
            remaining = [show for show in movie.showtimes if not show.expired]
            if remaining:
                movies.append(
                    MovieListing(
                        title=movie.title,
                        rating=movie.rating,
                        runtime_minutes=movie.runtime_minutes,
                        showtimes=remaining,
                    )
                )
        return TheatreDay(
            theatre=listing.theatre,
            date=listing.date,
            movies=movies,
            source=listing.source,
            showtimes_url=listing.showtimes_url,
        )
