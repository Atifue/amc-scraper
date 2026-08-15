from __future__ import annotations

import logging
import re
from datetime import date, datetime

from .models import MovieListing, Showtime, Theatre, TheatreDay

log = logging.getLogger(__name__)

TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", re.IGNORECASE)
ERROR_MARKERS = ("ERROR 500", "Global Safety Net", "queue.amctheatres.com")


async def fetch_theatre_day(theatre: Theatre, day: date, user_agent: str) -> TheatreDay:
    """Best-effort Playwright scrape of the public AMC showtimes page."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("playwright is not installed") from exc

    url = theatre.showtimes_url(day)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(user_agent=user_agent, locale="en-US")
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(8000)
            body = await page.inner_text("body")
        finally:
            await browser.close()

    if any(marker.lower() in body.lower() for marker in ERROR_MARKERS):
        raise RuntimeError(f"AMC website blocked or errored for {theatre.name}")

    movies = _parse_visible_listings(day, body)
    if not movies:
        raise RuntimeError(f"No showtimes parsed from AMC page for {theatre.name}")
    return TheatreDay(
        theatre=theatre,
        date=day,
        movies=movies,
        source="amc-web",
        showtimes_url=url,
    )


def _parse_visible_listings(day: date, body: str) -> list[MovieListing]:
    """Parse movie blocks from visible showtimes text.

    AMC's markup changes often; this looks for a title line followed by clock times.
    """
    movies: list[MovieListing] = []
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    skip_prefixes = (
        "today",
        "amc ",
        "change location",
        "showtimes",
        "privacy",
        "terms",
        "sign in",
        "join",
        "manage preferences",
        "we use cookies",
    )
    current_title: str | None = None
    current_times: list[Showtime] = []

    def flush() -> None:
        nonlocal current_title, current_times
        if current_title and current_times:
            movies.append(
                MovieListing(
                    title=current_title,
                    rating=None,
                    runtime_minutes=None,
                    showtimes=current_times,
                )
            )
        current_title = None
        current_times = []

    for line in lines:
        lowered = line.casefold()
        if any(lowered.startswith(prefix) for prefix in skip_prefixes):
            continue
        times = TIME_RE.findall(line)
        if times:
            for stamp in times:
                parsed = _parse_clock(day, stamp)
                if parsed is not None:
                    current_times.append(
                        Showtime(time_local=parsed, format_name="Standard")
                    )
            continue
        if current_title and current_times:
            flush()
        if 2 <= len(line) <= 80 and not line.startswith("http"):
            current_title = line

    flush()
    movies.sort(key=lambda item: item.title.casefold())
    return movies


def _parse_clock(day: date, stamp: str) -> datetime | None:
    cleaned = re.sub(r"\s+", " ", stamp.strip().upper())
    for fmt in ("%I:%M %p", "%I:%M%p"):
        try:
            return datetime.combine(day, datetime.strptime(cleaned, fmt).time())
        except ValueError:
            continue
    return None
