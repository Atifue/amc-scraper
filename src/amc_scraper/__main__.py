from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime
from pathlib import Path

from .client import AmcClient
from .config import Settings
from .fandango import today_in
from .formatter import listings_to_text, schedules_to_text
from .theatres import DAILY_THEATRES, THEATRES, get_theatre


def main() -> None:
    parser = argparse.ArgumentParser(description="Print AMC showtimes for configured theaters")
    parser.add_argument(
        "--theater",
        choices=["all", *[theatre.key for theatre in THEATRES]],
        default="all",
    )
    parser.add_argument("--date", help="YYYY-MM-DD (defaults to today in America/New_York)")
    parser.add_argument(
        "--coming",
        action="store_true",
        help="List unique movies scheduled as far ahead as listings go",
    )
    parser.add_argument(
        "--through",
        help="Optional last date for --coming as YYYY-MM-DD (default: until listings end)",
    )
    parser.add_argument(
        "--all-times",
        action="store_true",
        help="Include showtimes that have already started",
    )
    parser.add_argument(
        "--seats",
        action="store_true",
        help="Write a read-only Fandango seat map PNG (requires --movie and --time)",
    )
    parser.add_argument("--movie", help="Movie title for --seats")
    parser.add_argument("--time", help="Showtime for --seats, like 7:30 PM")
    parser.add_argument("--format", help="Optional format for --seats, like IMAX")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    theatres = list(DAILY_THEATRES) if args.theater == "all" else [get_theatre(args.theater)]
    settings = Settings.from_env(require_discord=False)
    client = AmcClient(settings)
    if args.seats:
        if args.theater == "all" or len(theatres) != 1:
            raise SystemExit("--seats needs --theater for one theater")
        if not args.movie or not args.time:
            raise SystemExit("--seats needs --movie and --time")
        from .client import ShowtimeError
        from .seats import SeatLookupError, render_seat_map_png, render_seat_map_summary

        day = _parse_date(args.date) if args.date else None
        try:
            movie, show, seat_map = asyncio.run(
                client.fetch_seat_map(
                    theatres[0], args.movie, args.time, day, args.format
                )
            )
        except (SeatLookupError, ShowtimeError) as exc:
            raise SystemExit(str(exc)) from exc
        print(f"{theatres[0].name} — {movie.title} · {args.time}")
        print(render_seat_map_summary(seat_map).replace("**", ""))
        out = Path("seats.png")
        out.write_bytes(render_seat_map_png(seat_map))
        print(f"Wrote {out.resolve()}")
        return
    if args.coming or args.through:
        start = today_in(theatres[0].timezone)
        end = _parse_date(args.through) if args.through else None
        schedules = asyncio.run(client.fetch_schedules(theatres, start, end))
        print(schedules_to_text(schedules))
        return
    day = _parse_date(args.date) if args.date else None
    listings = asyncio.run(
        client.fetch_many(theatres, day, remaining_only=not args.all_times)
    )
    print(listings_to_text(listings, remaining_only=not args.all_times))


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


if __name__ == "__main__":
    main()
