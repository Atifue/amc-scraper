from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .models import MovieListing, TheatreDay

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
        return [_embed(heading, description, listing)]

    payloads: list[dict] = []
    chunk: list[str] = []
    size = 0
    part = 1
    for block in blocks:
        extra = len(block) + (2 if chunk else 0)
        if chunk and size + extra > MAX_DESCRIPTION:
            title = heading if part == 1 else f"{heading} (cont.)"
            payloads.append(_embed(title, "\n\n".join(chunk), listing))
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
        payloads.append(_embed(title, "\n\n".join(chunk), listing))
    return payloads


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


def _embed(title: str, description: str, listing: TheatreDay) -> dict:
    return {
        "title": title,
        "description": description,
        "color": AMC_RED,
        "url": listing.showtimes_url,
        "footer": {"text": f"{listing.theatre.name} · {listing.source}"},
    }


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
