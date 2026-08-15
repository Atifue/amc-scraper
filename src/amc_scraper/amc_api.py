from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Any

import httpx

from .models import MovieListing, Showtime, Theatre, TheatreDay

log = logging.getLogger(__name__)

AMC_API_BASE = "https://api.amctheatres.com"


class AmcApiError(RuntimeError):
    pass


async def fetch_theatre_day(
    client: httpx.AsyncClient,
    theatre: Theatre,
    day: date,
    vendor_key: str,
) -> TheatreDay:
    theatre_id = theatre.amc_id or await _resolve_theatre_id(client, theatre, vendor_key)
    raw_showtimes = await _list_showtimes(client, theatre_id, day, vendor_key)
    movies = _group_showtimes(raw_showtimes)
    return TheatreDay(
        theatre=theatre,
        date=day,
        movies=movies,
        source="amc-api",
        showtimes_url=theatre.showtimes_url(day),
    )


async def _resolve_theatre_id(
    client: httpx.AsyncClient,
    theatre: Theatre,
    vendor_key: str,
) -> int:
    for candidate in (theatre.amc_slug, theatre.slug, theatre.path.split("/")[-1]):
        response = await client.get(
            f"{AMC_API_BASE}/v2/theatres/{candidate}",
            headers=_headers(vendor_key),
        )
        if response.status_code == 200:
            payload = response.json()
            theatre_id = payload.get("id")
            if theatre_id:
                log.info("Resolved %s to AMC theatre id %s via %s", theatre.key, theatre_id, candidate)
                return int(theatre_id)
        log.debug("Theatre lookup %s -> HTTP %s", candidate, response.status_code)
    raise AmcApiError(f"Could not resolve AMC theatre id for {theatre.name}")


async def _list_showtimes(
    client: httpx.AsyncClient,
    theatre_id: int,
    day: date,
    vendor_key: str,
) -> list[dict[str, Any]]:
    showtimes: list[dict[str, Any]] = []
    page = 1
    while page <= 20:
        response = await client.get(
            f"{AMC_API_BASE}/v2/theatres/{theatre_id}/showtimes/{day.isoformat()}",
            params={"page-number": page, "page-size": 100},
            headers=_headers(vendor_key),
        )
        if response.status_code >= 400:
            raise AmcApiError(
                f"AMC API showtimes failed ({response.status_code}): {response.text[:200]}"
            )
        payload = response.json()
        batch = (payload.get("_embedded") or {}).get("showtimes") or []
        showtimes.extend(batch)
        if not (payload.get("_links") or {}).get("next"):
            break
        page += 1
    return showtimes


def _group_showtimes(raw_showtimes: list[dict[str, Any]]) -> list[MovieListing]:
    grouped: dict[tuple[str, str | None, int | None], list[Showtime]] = defaultdict(list)
    for item in raw_showtimes:
        title = (item.get("movieName") or item.get("sortableMovieName") or "").strip()
        if not title:
            continue
        local = _parse_show_datetime(item.get("showDateTimeLocal"))
        if local is None:
            continue
        format_name = (item.get("premiumFormat") or "").strip() or "Standard"
        grouped[(title, item.get("mpaaRating") or None, item.get("runTime"))].append(
            Showtime(
                time_local=local,
                format_name=format_name,
                amenities=_attribute_names(item.get("attributes") or []),
                ticket_url=item.get("purchaseUrl") or item.get("movieUrl"),
                expired=bool(item.get("isCanceled")),
            )
        )
    movies = [
        MovieListing(
            title=title,
            rating=rating,
            runtime_minutes=runtime,
            showtimes=sorted(shows, key=lambda item: (item.format_name, item.time_local)),
        )
        for (title, rating, runtime), shows in grouped.items()
    ]
    movies.sort(key=lambda item: item.title.casefold())
    return movies


def _parse_show_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        log.debug("Could not parse AMC showDateTimeLocal %r", value)
        return None


def _attribute_names(attributes: list[dict[str, Any]]) -> tuple[str, ...]:
    names: list[str] = []
    for attr in attributes:
        name = (attr.get("name") or attr.get("code") or "").strip()
        if name:
            names.append(name)
    return tuple(dict.fromkeys(names))


def _headers(vendor_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-AMC-Vendor-Key": vendor_key,
    }
