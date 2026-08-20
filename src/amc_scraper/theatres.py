from .models import Theatre

FRESH_MEADOWS = Theatre(
    key="fresh-meadows",
    name="AMC Fresh Meadows 7",
    path="new-york-city/amc-fresh-meadows-7",
    slug="freshmeadows",
    fandango_id="aabtm",
    amc_slug="amc-fresh-meadows-7",
    fandango_slug="amc-loews-fresh-meadows-7",
)

BAY_TERRACE = Theatre(
    key="bay-terrace",
    name="AMC Bay Terrace 6",
    path="new-york-city/amc-bay-terrace-6",
    slug="bayterrace",
    fandango_id="aabqj",
    amc_slug="amc-bay-terrace-6",
    fandango_slug="amc-loews-bay-terrace-6",
)

LINCOLN_SQUARE = Theatre(
    key="lincoln-square",
    name="AMC Lincoln Square 13",
    path="new-york-city/amc-lincoln-square-13",
    slug="lincolnsquare",
    fandango_id="aabqi",
    amc_slug="amc-lincoln-square-13",
    fandango_slug="amc-lincoln-square-13",
)

THEATRES: tuple[Theatre, ...] = (FRESH_MEADOWS, BAY_TERRACE, LINCOLN_SQUARE)
DAILY_THEATRES: tuple[Theatre, ...] = (FRESH_MEADOWS, BAY_TERRACE)
THEATRES_BY_KEY = {theatre.key: theatre for theatre in THEATRES}


def get_theatre(key: str) -> Theatre:
    try:
        return THEATRES_BY_KEY[key]
    except KeyError as exc:
        known = ", ".join(THEATRES_BY_KEY)
        raise KeyError(f"Unknown theatre {key!r}. Known: {known}") from exc
