from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time

from .models import MovieListing, Showtime, TheatreDay

_CLOCK_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*(a|am|p|pm)?\s*$",
    re.IGNORECASE,
)
_ROW_LETTER_RE = re.compile(r"^([A-Za-z]+)")


@dataclass(frozen=True)
class Seat:
    layout_row: int
    display_row: str
    column: int
    available: bool
    wheelchair: bool
    label: str


@dataclass
class SeatMap:
    theater_name: str
    available: int
    total: int
    seats: list[Seat]


class SeatLookupError(ValueError):
    pass


def parse_clock_query(raw: str) -> time:
    text = raw.strip().lower().replace(".", "")
    text = text.replace(" ", "")
    match = _CLOCK_RE.match(text)
    if not match:
        raise SeatLookupError("Time must look like `7:30 PM` or `19:30`.")
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    suffix = (match.group(3) or "").lower()
    if suffix in {"p", "pm"}:
        if hour != 12:
            hour += 12
    elif suffix in {"a", "am"}:
        if hour == 12:
            hour = 0
    if hour > 23 or minute > 59:
        raise SeatLookupError("Time must look like `7:30 PM` or `19:30`.")
    return time(hour=hour, minute=minute)


def match_buyable_showtime(
    listing: TheatreDay,
    movie_query: str,
    clock: time,
    format_query: str | None = None,
) -> tuple[MovieListing, Showtime]:
    movies = _matching_movies(listing.movies, movie_query)
    if not movies:
        known = ", ".join(movie.title for movie in listing.movies[:8]) or "none listed"
        raise SeatLookupError(f"No movie matching {movie_query!r}. Playing: {known}.")
    if len(movies) > 1:
        titles = ", ".join(movie.title for movie in movies)
        raise SeatLookupError(f"Movie is ambiguous. Be more specific: {titles}.")

    movie = movies[0]
    matches = [
        show
        for show in movie.showtimes
        if show.time_local.hour == clock.hour and show.time_local.minute == clock.minute
    ]
    if format_query:
        needle = format_query.casefold()
        matches = [
            show
            for show in matches
            if needle in show.format_name.casefold()
            or any(needle in item.casefold() for item in show.amenities)
        ]
    if not matches:
        times = ", ".join(_format_clock(show.time_local) for show in movie.showtimes[:12]) or "none"
        raise SeatLookupError(f"No {movie.title} showtime at that clock. Listed: {times}.")
    buyable = [show for show in matches if show.buyable and show.showtime_hash]
    if not buyable:
        raise SeatLookupError(
            "That showtime is not on sale yet, so Fandango has no seat map."
        )
    if len(buyable) > 1:
        labels = " · ".join(show.format_name for show in buyable)
        raise SeatLookupError(f"Several formats at that time. Pass format: {labels}.")
    return movie, buyable[0]


def parse_seat_map(payload: dict) -> SeatMap:
    raw_seats = payload.get("seats") or []
    seats: list[Seat] = []
    for raw in raw_seats:
        column = _as_int(raw.get("column"))
        layout_row = _as_int(raw.get("row"))
        if column is None or layout_row is None:
            continue
        label = str(raw.get("id") or f"{layout_row}-{column}")
        seat_type = str(raw.get("type") or "")
        attrs = raw.get("attributes")
        wheelchair = "wheelchair" in seat_type.casefold() or (
            "wheelchair" in str(attrs).casefold() if attrs else False
        )
        seats.append(
            Seat(
                layout_row=layout_row,
                display_row=_display_row(label, layout_row),
                column=column,
                available=str(raw.get("status") or "").upper() == "A",
                wheelchair=wheelchair,
                label=label,
            )
        )
    seats.sort(key=lambda seat: (seat.layout_row, seat.column))
    available = sum(1 for seat in seats if seat.available)
    total = int(payload.get("totalSeatCount") or len(seats))
    return SeatMap(
        theater_name=str(payload.get("theaterName") or ""),
        available=int(payload.get("totalAvailableSeatCount") or available),
        total=total,
        seats=seats,
    )


def render_seat_map_text(seat_map: SeatMap) -> str:
    if not seat_map.seats:
        return "No seats in the Fandango map."
    by_row: dict[int, list[Seat]] = defaultdict(list)
    labels: dict[int, str] = {}
    for seat in seat_map.seats:
        by_row[seat.layout_row].append(seat)
        labels[seat.layout_row] = seat.display_row
    columns = [seat.column for seat in seat_map.seats]
    min_col, max_col = min(columns), max(columns)
    row_width = max(len(labels[row]) for row in by_row)
    compact = (max_col - min_col) > 22
    lines = ["SCREEN"]
    for layout_row in sorted(by_row):
        present = {seat.column: seat for seat in by_row[layout_row]}
        cells: list[str] = []
        for column in range(min_col, max_col + 1):
            cells.append(_glyph(present.get(column)))
            if not compact and column < max_col and (column - min_col + 1) % 4 == 0:
                cells.append(" ")
        lines.append(f"{labels[layout_row]:>{row_width}} {''.join(cells).rstrip()}")
    groups = _open_groups(seat_map, limit=5)
    group_text = ", ".join(groups) if groups else "none"
    legend = "□ open  ☒ taken  ▣ wheelchair  ▦ taken wheelchair"
    return (
        f"{seat_map.available}/{seat_map.total} open\n"
        f"Biggest open blocks: {group_text}\n"
        f"{legend}\n\n"
        + "\n".join(lines)
    )


def _matching_movies(movies: list[MovieListing], query: str) -> list[MovieListing]:
    needle = query.strip().casefold()
    if not needle:
        return []
    exact = [movie for movie in movies if movie.title.casefold() == needle]
    if exact:
        return exact
    starts = [movie for movie in movies if movie.title.casefold().startswith(needle)]
    if len(starts) == 1:
        return starts
    contains = [movie for movie in movies if needle in movie.title.casefold()]
    if len(contains) == 1:
        return contains
    return starts or contains


def _display_row(label: str, layout_row: int) -> str:
    match = _ROW_LETTER_RE.match(label)
    if match:
        return match.group(1).upper()
    return str(layout_row)


def _glyph(seat: Seat | None) -> str:
    if seat is None:
        return " "
    if seat.wheelchair:
        return "▣" if seat.available else "▦"
    return "□" if seat.available else "☒"


def _open_groups(seat_map: SeatMap, *, limit: int) -> list[str]:
    by_row: dict[str, list[int]] = defaultdict(list)
    for seat in seat_map.seats:
        if seat.available:
            by_row[seat.display_row].append(seat.column)
    groups: list[tuple[int, str]] = []
    for row, columns in by_row.items():
        columns.sort()
        start = prev = columns[0]
        for column in columns[1:]:
            if column == prev + 1:
                prev = column
                continue
            groups.append(_group_tuple(row, start, prev))
            start = prev = column
        groups.append(_group_tuple(row, start, prev))
    groups.sort(key=lambda item: (-item[0], item[1]))
    return [label for _, label in groups[:limit]]


def _group_tuple(row: str, start: int, end: int) -> tuple[int, str]:
    size = end - start + 1
    if start == end:
        label = f"{row}{start} ({size})"
    else:
        label = f"{row}{start}-{end} ({size})"
    return size, label


def _format_clock(value: datetime) -> str:
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}"


def _as_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
