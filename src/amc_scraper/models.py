from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class Theatre:
    key: str
    name: str
    path: str
    slug: str
    fandango_id: str
    amc_slug: str
    amc_id: int | None = None
    timezone: str = "America/New_York"

    def showtimes_url(self, day: date) -> str:
        return (
            "https://www.amctheatres.com/movie-theatres/"
            f"{self.path}/showtimes?date={day.isoformat()}"
        )


@dataclass(frozen=True)
class Showtime:
    time_local: datetime
    format_name: str
    amenities: tuple[str, ...] = ()
    ticket_url: str | None = None
    expired: bool = False


@dataclass
class MovieListing:
    title: str
    rating: str | None
    runtime_minutes: int | None
    showtimes: list[Showtime] = field(default_factory=list)


@dataclass
class TheatreDay:
    theatre: Theatre
    date: date
    movies: list[MovieListing]
    source: str
    showtimes_url: str
