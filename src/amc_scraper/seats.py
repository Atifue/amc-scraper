from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, time
from io import BytesIO
from pathlib import Path

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
    number: int | None = None
    x: float | None = None
    y: float | None = None
    width: float = 24.0
    height: float = 24.0


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
                number=_seat_number(label),
                x=_as_float(raw.get("x")),
                y=_as_float(raw.get("y")),
                width=_as_float(raw.get("width")) or 24.0,
                height=_as_float(raw.get("height")) or 24.0,
            )
        )
    seats = _snap_shared_rows(seats)
    seats.sort(key=lambda seat: (seat.layout_row, seat.column))
    available = sum(1 for seat in seats if seat.available)
    total = int(payload.get("totalSeatCount") or len(seats))
    return SeatMap(
        theater_name=str(payload.get("theaterName") or ""),
        available=int(payload.get("totalAvailableSeatCount") or available),
        total=total,
        seats=seats,
    )


def render_seat_map_png(seat_map: SeatMap) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is required to draw seat maps") from exc

    seats = seat_map.seats
    if not seats:
        image = Image.new("RGB", (640, 200), "#111114")
        ImageDraw.Draw(image).text((24, 88), "No seats in the Fandango map.", fill="#d0d0d6")
        return _png_bytes(image)

    placed = _seats_with_geometry(seats)
    min_x = min(seat.x or 0 for seat in placed)
    min_y = min(seat.y or 0 for seat in placed)
    max_x = max((seat.x or 0) + seat.width for seat in placed)
    max_y = max((seat.y or 0) + seat.height for seat in placed)
    src_w = max(max_x - min_x, 1)
    src_h = max(max_y - min_y, 1)

    label_w = 52
    pad = 36
    screen_h = 28
    legend_h = 44
    target_w = 1400
    scale = (target_w - pad * 2 - label_w * 2) / src_w
    map_h = src_h * scale
    if map_h > 980:
        scale *= 980 / map_h
        map_h = 980
    img_w = int(pad * 2 + label_w * 2 + src_w * scale)
    img_h = int(pad + 8 + screen_h + 28 + map_h + pad + legend_h)

    image = Image.new("RGB", (img_w, img_h), "#0f0f13")
    draw = ImageDraw.Draw(image)
    title_font = _font(16, bold=True)
    row_font = _font(18, bold=True)
    number_font = _font(15, bold=True)
    legend_font = _font(14)

    origin_x = pad + label_w
    origin_y = pad + 8 + screen_h + 28
    map_right = origin_x + src_w * scale

    _draw_screen(draw, origin_x, map_right, pad + 6, screen_h, title_font)

    row_ys: dict[str, list[float]] = defaultdict(list)
    for seat in placed:
        x0 = origin_x + ((seat.x or 0) - min_x) * scale
        y0 = origin_y + ((seat.y or 0) - min_y) * scale
        x1 = x0 + seat.width * scale
        y1 = y0 + seat.height * scale
        _draw_seat(draw, seat, x0, y0, x1, y1, number_font)
        row_ys[seat.display_row].append((y0 + y1) / 2)

    for row, mids in row_ys.items():
        mid = sum(mids) / len(mids)
        tw = _text_width(draw, row, row_font)
        draw.text((pad + (label_w - tw) / 2, mid - 10), row, fill="#c5c5ce", font=row_font)
        draw.text((map_right + (label_w - tw) / 2, mid - 10), row, fill="#c5c5ce", font=row_font)

    _draw_legend(draw, pad, img_h - pad - 6, legend_font)
    return _png_bytes(image)


def render_seat_map_summary(seat_map: SeatMap) -> str:
    groups = _open_groups(seat_map, limit=4)
    group_text = ", ".join(groups) if groups else "none"
    return (
        f"**{seat_map.available}/{seat_map.total} open**\n"
        f"Green = open · Gray = taken · Blue = wheelchair\n"
        f"Biggest open blocks: {group_text}"
    )


def _seats_with_geometry(seats: list[Seat]) -> list[Seat]:
    if all(seat.x is not None and seat.y is not None for seat in seats):
        return seats
    gap = 6.0
    size = 22.0
    by_row: dict[int, list[Seat]] = defaultdict(list)
    for seat in seats:
        by_row[seat.layout_row].append(seat)
    placed: list[Seat] = []
    for r_index, layout_row in enumerate(sorted(by_row)):
        row_seats = sorted(by_row[layout_row], key=lambda item: item.column)
        for c_index, seat in enumerate(row_seats):
            placed.append(
                replace(
                    seat,
                    x=c_index * (size + gap),
                    y=r_index * (size + gap),
                    width=size,
                    height=size,
                )
            )
    return placed


def _draw_screen(draw, left: float, right: float, top: float, height: float, font) -> None:
    inset = (right - left) * 0.05
    draw.polygon(
        [
            (left + inset, top),
            (right - inset, top),
            (right, top + height),
            (left, top + height),
        ],
        fill="#ececf1",
    )
    label = "SCREEN"
    tw = _text_width(draw, label, font)
    draw.text(((left + right - tw) / 2, top + 6), label, fill="#1b1b20", font=font)


def _draw_seat(draw, seat: Seat, x0: float, y0: float, x1: float, y1: float, font) -> None:
    width = x1 - x0
    height = y1 - y0
    radius = max(4, int(min(width, height) * 0.32))
    draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=_seat_color(seat))
    label = "" if seat.number is None else str(seat.number)
    if not label or width < 22 or height < 18:
        return
    tw = _text_width(draw, label, font)
    box = draw.textbbox((0, 0), label, font=font)
    th = box[3] - box[1]
    draw.text(
        (x0 + (width - tw) / 2, y0 + (height - th) / 2 - box[1]),
        label,
        fill=_seat_text_color(seat),
        font=font,
    )


def _seat_color(seat: Seat) -> str:
    if seat.wheelchair:
        return "#5eb0ff" if seat.available else "#3d5a78"
    return "#3ee08c" if seat.available else "#4a4a54"


def _seat_text_color(seat: Seat) -> str:
    if seat.available:
        return "#082016" if not seat.wheelchair else "#071625"
    return "#f4f6fa"


def _draw_legend(draw, x: int, y: int, font) -> None:
    items = [
        ("#3ee08c", "Open"),
        ("#4a4a54", "Taken"),
        ("#5eb0ff", "Wheelchair"),
    ]
    cursor = x
    for color, label in items:
        draw.rounded_rectangle((cursor, y - 10, cursor + 16, y + 6), radius=5, fill=color)
        draw.text((cursor + 22, y - 11), label, fill="#c8c8d0", font=font)
        cursor += 108


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    regular = (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    )
    bold_paths = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    )
    paths = bold_paths + regular if bold else regular
    for path in paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _text_width(draw, text: str, font) -> float:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _png_bytes(image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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
        prefix = match.group(1).upper()
        if prefix and prefix != "WC":
            return prefix
    return str(layout_row)


def _seat_number(label: str) -> int | None:
    match = re.search(r"(\d+)$", label)
    if not match:
        return None
    return int(match.group(1))


def _snap_shared_rows(seats: list[Seat]) -> list[Seat]:
    anchors = [seat for seat in seats if seat.y is not None and seat.display_row.isalpha()]
    if not anchors:
        return seats
    snapped: list[Seat] = []
    for seat in seats:
        if seat.y is None or seat.display_row.isalpha():
            snapped.append(seat)
            continue
        nearest = min(anchors, key=lambda other: abs((other.y or 0) - seat.y))
        if abs((nearest.y or 0) - seat.y) <= max(seat.height, 20.0):
            snapped.append(replace(seat, display_row=nearest.display_row))
        else:
            snapped.append(seat)
    return snapped


def _open_groups(seat_map: SeatMap, *, limit: int) -> list[str]:
    by_row: dict[str, list[int]] = defaultdict(list)
    for seat in seat_map.seats:
        if seat.available and seat.number is not None:
            by_row[seat.display_row].append(seat.number)
    groups: list[tuple[int, str]] = []
    for row, numbers in by_row.items():
        numbers.sort()
        start = prev = numbers[0]
        for number in numbers[1:]:
            if number == prev + 1:
                prev = number
                continue
            groups.append(_group_tuple(row, start, prev))
            start = prev = number
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
