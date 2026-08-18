from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .models import TheatreDay

log = logging.getLogger(__name__)

DEFAULT_SEEN_PATH = Path("data/seen_showtimes.json")
SEEN_VERSION = 2


@dataclass(frozen=True)
class WatchedShowtime:
    title: str
    date: date
    time_local: datetime
    format_name: str

    def key(self) -> str:
        clock = self.time_local.strftime("%H:%M")
        return f"{self.title}|{self.date.isoformat()}|{clock}|{self.format_name}"


def showtimes_from_listings(
    listings: list[TheatreDay | None],
    *,
    buyable_only: bool = False,
) -> list[WatchedShowtime]:
    items: list[WatchedShowtime] = []
    for listing in listings:
        if listing is None:
            continue
        for movie in listing.movies:
            for show in movie.showtimes:
                if buyable_only and not show.buyable:
                    continue
                items.append(
                    WatchedShowtime(
                        title=movie.title,
                        date=listing.date,
                        time_local=show.time_local,
                        format_name=show.format_name or "Standard",
                    )
                )
    return items


def load_seen(path: Path = DEFAULT_SEEN_PATH) -> tuple[bool, set[str]]:
    if not path.exists():
        return False, set()
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("Could not read %s; treating as first poll", path)
        return False, set()
    if payload.get("version") != SEEN_VERSION:
        log.info("Seen-file version is %s; rebasing buyable showtimes", payload.get("version"))
        return False, set()
    keys = payload.get("keys")
    if not isinstance(keys, list):
        return False, set()
    return True, {str(key) for key in keys}


def save_seen(keys: set[str], path: Path = DEFAULT_SEEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": SEEN_VERSION, "keys": sorted(keys)}
    path.write_text(json.dumps(payload, indent=2) + "\n")
