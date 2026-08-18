from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta

import httpx

from . import amc_api, amc_web
from .config import Settings
from .fandango import USER_AGENT, parse_fandango_payload, today_in
from .models import MovieListing, ScheduledMovie, Theatre, TheatreDay, TheatreSchedule
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
        self._schedule_cache: dict[tuple[str, str, str], tuple[float, TheatreSchedule]] = {}

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

    async def fetch_schedule(
        self,
        theatre: Theatre | str,
        start: date | None = None,
        end: date | None = None,
        *,
        use_cache: bool = True,
    ) -> TheatreSchedule:
        if isinstance(theatre, str):
            theatre = get_theatre(theatre)
        start = start or today_in(theatre.timezone)
        if end is not None and end < start:
            raise ShowtimeError("End date must be on or after the start date")

        if use_cache:
            cached = self._get_schedule_cached(theatre, start, end)
            if cached is not None:
                return cached

        listings = await self._scan_schedule_days(
            theatre, start, end, use_cache=use_cache
        )
        movies = _aggregate_schedule(listings)
        visible_end = max((movie.last_date for movie in movies), default=start)
        schedule = TheatreSchedule(
            theatre=theatre,
            start=start,
            end=visible_end,
            movies=movies,
            source="fandango",
            showtimes_url=theatre.showtimes_url(start),
        )
        if use_cache:
            self._store_schedule_cached(theatre, start, end, schedule)
        return schedule

    async def fetch_schedule_listings(
        self,
        theatre: Theatre | str,
        start: date | None = None,
        end: date | None = None,
        *,
        use_cache: bool = True,
    ) -> list[TheatreDay | None]:
        if isinstance(theatre, str):
            theatre = get_theatre(theatre)
        start = start or today_in(theatre.timezone)
        if end is not None and end < start:
            raise ShowtimeError("End date must be on or after the start date")
        return await self._scan_schedule_days(
            theatre, start, end, use_cache=use_cache
        )

    async def _scan_schedule_days(
        self,
        theatre: Theatre,
        start: date,
        end: date | None,
        *,
        use_cache: bool = True,
    ) -> list[TheatreDay | None]:
        horizon = start + timedelta(days=_MAX_HORIZON_DAYS)
        cap = min(end, horizon) if end is not None else horizon
        listings: list[TheatreDay | None] = []
        empty_streak = 0
        current = start
        semaphore = asyncio.Semaphore(6)
        async with self._http() as client:
            while current <= cap:
                batch_end = min(current + timedelta(days=_SCAN_BATCH_DAYS - 1), cap)
                batch_days = _date_range(current, batch_end)
                batch = await asyncio.gather(
                    *[
                        self._fetch_schedule_day(
                            client, semaphore, theatre, day, use_cache=use_cache
                        )
                        for day in batch_days
                    ]
                )
                listings.extend(batch)
                for listing in batch:
                    if listing is not None and any(movie.showtimes for movie in listing.movies):
                        empty_streak = 0
                    else:
                        empty_streak += 1
                scanned = (batch_end - start).days + 1
                if empty_streak >= _EMPTY_STOP_DAYS and scanned >= _MIN_SCAN_DAYS:
                    break
                current = batch_end + timedelta(days=1)
        return listings

    async def fetch_schedules(
        self,
        theatres: list[Theatre] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[TheatreSchedule]:
        theatres = theatres or list(THEATRES)
        schedules: list[TheatreSchedule] = []
        for index, theatre in enumerate(theatres):
            if index:
                await asyncio.sleep(self.settings.inter_theatre_delay)
            schedules.append(await self.fetch_schedule(theatre, start, end))
        return schedules

    async def _fetch_schedule_day(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        theatre: Theatre,
        day: date,
        *,
        use_cache: bool = True,
    ) -> TheatreDay | None:
        if use_cache:
            cached = self._get_cached(theatre, day)
            if cached is not None:
                return cached
        async with semaphore:
            try:
                listing = await self._fetch_fandango(theatre, day, client=client)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    raise
                log.warning("Schedule fetch failed for %s %s: %s", theatre.key, day, exc)
                return None
            except Exception as exc:
                log.warning("Schedule fetch failed for %s %s: %s", theatre.key, day, exc)
                return None
        if use_cache:
            self._store_cached(theatre, day, listing)
        return listing

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

    async def _fetch_fandango(
        self,
        theatre: Theatre,
        day: date,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> TheatreDay:
        url = (
            "https://www.fandango.com/napi/theaterMovieShowtimes/"
            f"{theatre.fandango_id}?startDate={day.isoformat()}&isdesktop=true"
        )
        page_slug = theatre.fandango_slug or theatre.path.split("/")[-1]
        referer = f"https://www.fandango.com/{page_slug}-{theatre.fandango_id}/theater-page"
        headers = {
            "Accept": "application/json",
            "Referer": referer,
            "Origin": "https://www.fandango.com",
        }
        if client is None:
            async with self._http() as owned:
                response = await owned.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
        else:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        return parse_fandango_payload(theatre, day, payload)

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

    def _get_schedule_cached(
        self, theatre: Theatre, start: date, end: date | None
    ) -> TheatreSchedule | None:
        key = _schedule_cache_key(theatre, start, end)
        item = self._schedule_cache.get(key)
        if item is None:
            return None
        expires_at, schedule = item
        if time.monotonic() > expires_at:
            self._schedule_cache.pop(key, None)
            return None
        return schedule

    def _store_schedule_cached(
        self,
        theatre: Theatre,
        start: date,
        end: date | None,
        schedule: TheatreSchedule,
    ) -> None:
        self._schedule_cache[_schedule_cache_key(theatre, start, end)] = (
            time.monotonic() + 6 * 60 * 60,
            schedule,
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


_MAX_HORIZON_DAYS = 548
_MIN_SCAN_DAYS = 90
_EMPTY_STOP_DAYS = 120
_SCAN_BATCH_DAYS = 14


def _schedule_cache_key(theatre: Theatre, start: date, end: date | None) -> tuple[str, str, str]:
    return (theatre.key, start.isoformat(), end.isoformat() if end else "auto")


def _date_range(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _aggregate_schedule(listings: list[TheatreDay | None]) -> list[ScheduledMovie]:
    grouped: dict[str, dict] = {}
    for listing in listings:
        if listing is None:
            continue
        for movie in listing.movies:
            if not movie.showtimes:
                continue
            key = movie.title.casefold()
            item = grouped.get(key)
            if item is None:
                grouped[key] = {
                    "title": movie.title,
                    "rating": movie.rating,
                    "runtime_minutes": movie.runtime_minutes,
                    "first": listing.date,
                    "last": listing.date,
                    "days": 1,
                }
            else:
                item["first"] = min(item["first"], listing.date)
                item["last"] = max(item["last"], listing.date)
                item["days"] += 1
                if not item["rating"] and movie.rating:
                    item["rating"] = movie.rating
                if not item["runtime_minutes"] and movie.runtime_minutes:
                    item["runtime_minutes"] = movie.runtime_minutes
    movies = [
        ScheduledMovie(
            title=item["title"],
            rating=item["rating"],
            runtime_minutes=item["runtime_minutes"],
            first_date=item["first"],
            last_date=item["last"],
            days_playing=item["days"],
        )
        for item in grouped.values()
    ]
    movies.sort(key=lambda movie: (movie.first_date, movie.title.casefold()))
    return movies
