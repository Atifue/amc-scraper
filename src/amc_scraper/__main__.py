from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime

from .client import AmcClient
from .config import Settings
from .fandango import today_in
from .formatter import listings_to_text, schedules_to_text
from .theatres import THEATRES, get_theatre


def main() -> None:
    parser = argparse.ArgumentParser(description="Print AMC showtimes for Fresh Meadows and Bay Terrace")
    parser.add_argument(
        "--theater",
        choices=["both", *[theatre.key for theatre in THEATRES]],
        default="both",
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    theatres = list(THEATRES) if args.theater == "both" else [get_theatre(args.theater)]
    settings = Settings.from_env(require_discord=False)
    client = AmcClient(settings)
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
