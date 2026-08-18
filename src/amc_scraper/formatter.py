from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from .models import MovieListing, ScheduledMovie, Theatre, TheatreDay, TheatreSchedule
from .watch import WatchedShowtime

# Discord embed limits (leave headroom for formatting)
MAX_DESCRIPTION = 3900
MAX_EMBEDS = 10
AMC_RED = 0xE31837


def listing_to_embed_payloads(
    listing: TheatreDay,
    *,
    remaining_only: bool = True,
) -> list[dict]:
    """Return kwargs dicts suitable for discord.Embed."""
    heading = _heading(listing)
    blocks = [_movie_block(movie, remaining_only=remaining_only) for movie in listing.movies]
    blocks = [block for block in blocks if block]
    if not blocks:
        description = _empty_description(listing, remaining_only=remaining_only)
        return [_embed(heading, description, listing.showtimes_url, f"{listing.theatre.name} · {listing.source}")]
    return _chunk_embeds(
        heading,
        blocks,
        listing.showtimes_url,
        f"{listing.theatre.name} · {listing.source}",
    )


def schedule_to_embed_payloads(schedule: TheatreSchedule) -> list[dict]:
    heading = (
        f"{schedule.theatre.name} — through "
        f"{_short_date(schedule.end)}"
    )
    blocks = [_scheduled_movie_block(movie) for movie in schedule.movies]
    if not blocks:
        return [
            _embed(
                heading,
                f"No movies scheduled from {_short_date(schedule.start)} to {_short_date(schedule.end)}.",
                url=schedule.showtimes_url,
                footer=f"{schedule.theatre.name} · {schedule.source}",
            )
        ]
    return _chunk_embeds(heading, blocks, schedule.showtimes_url, f"{schedule.theatre.name} · {schedule.source}")


def schedules_to_text(schedules: list[TheatreSchedule]) -> str:
    parts: list[str] = []
    for schedule in schedules:
        heading = f"{schedule.theatre.name} — through {_short_date(schedule.end)}"
        lines = [heading]
        blocks = [_scheduled_movie_block(movie) for movie in schedule.movies]
        if not blocks:
            lines.append(
                f"No movies scheduled from {_short_date(schedule.start)} to {_short_date(schedule.end)}."
            )
        else:
            lines.extend(blocks)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def new_showtimes_to_embed_payloads(
    theatre: Theatre,
    items: list[WatchedShowtime],
) -> list[dict]:
    heading = f"New showtimes at {theatre.name}"
    blocks = _new_showtime_blocks(items)
    if not blocks:
        return []
    return _chunk_embeds(
        heading,
        blocks,
        theatre.showtimes_url(items[0].date),
        theatre.name,
    )


def _new_showtime_blocks(items: list[WatchedShowtime]) -> list[str]:
    grouped: dict[str, dict[date, dict[str, list[datetime]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for item in items:
        grouped[item.title][item.date][item.format_name].append(item.time_local)
    blocks: list[str] = []
    for title in sorted(grouped, key=str.casefold):
        lines = [f"**{title}**"]
        for day in sorted(grouped[title]):
            lines.append(_short_date(day))
            for format_name in grouped[title][day]:
                clocks = grouped[title][day][format_name]
                unique = list(dict.fromkeys(_format_clock(clock) for clock in sorted(clocks)))
                lines.append(f"{format_name}: {' · '.join(unique)}")
        blocks.append("\n".join(lines))
    return blocks


def listings_to_text(listings: list[TheatreDay], *, remaining_only: bool = True) -> str:
    parts: list[str] = []
    for listing in listings:
        lines = [_heading(listing)]
        blocks = [_movie_block(movie, remaining_only=remaining_only) for movie in listing.movies]
        blocks = [block for block in blocks if block]
        if not blocks:
            lines.append(_empty_description(listing, remaining_only=remaining_only))
        else:
            lines.extend(blocks)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _chunk_embeds(heading: str, blocks: list[str], url: str, footer: str) -> list[dict]:
    payloads: list[dict] = []
    chunk: list[str] = []
    size = 0
    part = 1
    for block in blocks:
        extra = len(block) + (2 if chunk else 0)
        if chunk and size + extra > MAX_DESCRIPTION:
            title = heading if part == 1 else f"{heading} (cont.)"
            payloads.append(_embed(title, "\n\n".join(chunk), url, footer))
            chunk = [block]
            size = len(block)
            part += 1
            if len(payloads) >= MAX_EMBEDS:
                break
        else:
            chunk.append(block)
            size += extra
    if chunk and len(payloads) < MAX_EMBEDS:
        title = heading if part == 1 else f"{heading} (cont.)"
        payloads.append(_embed(title, "\n\n".join(chunk), url, footer))
    return payloads


def _embed(title: str, description: str, url: str, footer: str) -> dict:
    return {
        "title": title,
        "description": description,
        "color": AMC_RED,
        "url": url,
        "footer": {"text": footer},
    }


def _scheduled_movie_block(movie: ScheduledMovie) -> str:
    meta = " · ".join(
        part
        for part in (_format_rating(movie.rating), _format_runtime(movie.runtime_minutes))
        if part
    )
    header = f"**{movie.title}**" + (f" — {meta}" if meta else "")
    if movie.first_date == movie.last_date:
        span = _short_date(movie.first_date)
    else:
        span = f"{_short_date(movie.first_date)} – {_short_date(movie.last_date)}"
    return f"{header}\n{span}"


def _short_date(value: datetime | date) -> str:
    return value.strftime("%b ") + str(value.day)


def _heading(listing: TheatreDay) -> str:
    stamped = listing.date.strftime("%a %b ") + str(listing.date.day)
    return f"{listing.theatre.name} — {stamped}"


def _empty_description(listing: TheatreDay, *, remaining_only: bool) -> str:
    if remaining_only:
        return "No remaining showtimes today."
    return f"No showtimes listed for {listing.date.isoformat()}."


def _movie_block(movie: MovieListing, *, remaining_only: bool) -> str:
    showtimes = [
        show for show in movie.showtimes if not remaining_only or not show.expired
    ]
    if not showtimes:
        return ""
    meta = " · ".join(part for part in (_format_rating(movie.rating), _format_runtime(movie.runtime_minutes)) if part)
    header = f"**{movie.title}**" + (f" — {meta}" if meta else "")
    grouped: dict[str, list[str]] = defaultdict(list)
    for show in showtimes:
        label = _show_label(show.format_name, show.amenities)
        grouped[label].append(_format_clock(show.time_local))
    lines = [header]
    for label, clocks in grouped.items():
        unique_clocks = list(dict.fromkeys(clocks))
        lines.append(f"{label}: {' · '.join(unique_clocks)}")
    return "\n".join(lines)


def _show_label(format_name: str, amenities: tuple[str, ...]) -> str:
    extras = [item for item in amenities if item.casefold() not in {format_name.casefold(), "standard"}]
    if extras:
        return f"{format_name} ({', '.join(extras)})"
    return format_name or "Standard"


def _format_clock(value: datetime) -> str:
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}"


def _format_runtime(minutes: int | None) -> str | None:
    if not minutes:
        return None
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _format_rating(rating: str | None) -> str | None:
    if not rating:
        return None
    return rating.replace("PG13", "PG-13").replace("NC17", "NC-17")
