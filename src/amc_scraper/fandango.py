from __future__ import annotations

import logging
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .models import MovieListing, Showtime, Theatre, TheatreDay

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

GENERIC_AMENITIES = {
    "recliner seats",
    "reserved seating",
    "closed caption",
    "closed captioning",
    "accessibility devices available",
    "laser at amc",
    "reald 3d",
}

YEAR_SUFFIX = re.compile(r"\s*\(\d{4}\)\s*$")


def parse_fandango_payload(theatre: Theatre, day: date, payload: dict) -> TheatreDay:
    view = payload.get("viewModel") or payload
    movies: list[MovieListing] = []
    for movie in view.get("movies") or []:
        title = YEAR_SUFFIX.sub("", (movie.get("title") or "").strip())
        if not title:
            continue
        showtimes: list[Showtime] = []
        for variant in movie.get("variants") or []:
            for group in variant.get("amenityGroups") or []:
                format_name, amenities = _format_and_amenities(variant, group)
                for raw in group.get("showtimes") or []:
                    showtime = _parse_showtime(day, format_name, amenities, raw)
                    if showtime is not None:
                        showtimes.append(showtime)
        showtimes.sort(key=lambda item: (item.format_name, item.time_local))
        movies.append(
            MovieListing(
                title=title,
                rating=_clean_rating(movie.get("rating")),
                runtime_minutes=_as_int(movie.get("runtime")),
                showtimes=showtimes,
            )
        )
    movies.sort(key=lambda item: item.title.casefold())
    return TheatreDay(
        theatre=theatre,
        date=day,
        movies=movies,
        source="fandango",
        showtimes_url=theatre.showtimes_url(day),
    )


def _format_and_amenities(variant: dict, group: dict) -> tuple[str, tuple[str, ...]]:
    format_name = (variant.get("filmFormatHeader") or "Standard").strip() or "Standard"
    labels: list[str] = []
    for amenity in group.get("amenities") or []:
        name = (amenity.get("name") or "").strip()
        if not name:
            continue
        lowered = name.casefold()
        if lowered in GENERIC_AMENITIES:
            continue
        if lowered.startswith("imax"):
            format_name = name
            continue
        labels.append(name)
    if group.get("isDolby") and "dolby" not in format_name.casefold():
        format_name = "Dolby Cinema"
    return format_name, tuple(dict.fromkeys(labels))


def _parse_showtime(
    day: date,
    format_name: str,
    amenities: tuple[str, ...],
    raw: dict,
) -> Showtime | None:
    local = _parse_local_time(day, raw)
    if local is None:
        return None
    expired = bool(raw.get("expired")) or raw.get("type") == "pastshowtime"
    ticket_url = raw.get("ticketingJumpPageURL") or None
    return Showtime(
        time_local=local,
        format_name=format_name,
        amenities=amenities,
        ticket_url=ticket_url,
        expired=expired,
    )


def _parse_local_time(day: date, raw: dict) -> datetime | None:
    ticketing = raw.get("ticketingDate")
    if isinstance(ticketing, str) and "+" in ticketing:
        date_part, time_part = ticketing.split("+", 1)
        try:
            parsed_date = date.fromisoformat(date_part)
            hour_s, minute_s = time_part.split(":", 1)
            return datetime.combine(parsed_date, time(int(hour_s), int(minute_s[:2])))
        except ValueError:
            log.debug("Could not parse ticketingDate %r", ticketing)

    readable = raw.get("screenReaderTime") or raw.get("date")
    if not isinstance(readable, str):
        return None
    candidates = [readable.strip(), readable.strip().upper()]
    compact = readable.strip().upper().replace(" ", "")
    if compact.endswith("A") or compact.endswith("P"):
        candidates.append(f"{compact[:-1]} {compact[-1]}M")
    elif compact.endswith("AM") or compact.endswith("PM"):
        candidates.append(f"{compact[:-2]} {compact[-2:]}")
    for candidate in candidates:
        for fmt in ("%I:%M %p", "%I:%M%p"):
            try:
                return datetime.combine(day, datetime.strptime(candidate, fmt).time())
            except ValueError:
                continue
    log.debug("Could not parse showtime %r", readable)
    return None


def _clean_rating(value: object) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"not rated", "nr", "unrated"}:
        return "NR"
    return text.replace("-", "") if text.upper().replace("-", "") in {"PG", "PG13", "NC17", "G", "R"} else text


def _as_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def today_in(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()
