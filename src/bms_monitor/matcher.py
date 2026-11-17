"""Which versions to fetch and which shows match a watch."""

from __future__ import annotations

from .bms import Show, Variant
from .config import Watch


def _has(haystack: str, needles: list[str]) -> bool:
    h = haystack.lower()
    return any(n.lower() in h for n in needles)


def variants_to_check(watch: Watch, variants: list[Variant]) -> list[Variant]:
    """Versions worth fetching showtimes for.

    Languages narrow the list. Formats narrow it only when every format the
    watch asks for is a version of its own (IMAX, 4DX, 3D...). Screen types
    such as LASER or ATMOS live inside a version, so those need all of them.
    """
    vs = [v for v in variants if not watch.languages or not v.language or _has(v.language, watch.languages)]
    if watch.formats and all(any(f.lower() in v.format.lower() for v in vs) for f in watch.formats):
        vs = [v for v in vs if _has(v.format, watch.formats)]
    return vs


def _at(show: Show, cinemas: list[str]) -> bool:
    return any(c.lower() in show.venue_name.lower() or c.upper() == show.venue_code for c in cinemas)


def show_matches(watch: Watch, variant: Variant, show: Show) -> bool:
    if not show.available:
        return False
    if watch.cinemas and not _at(show, watch.cinemas):
        return False
    return _in_filters(watch, variant, show)


def show_reopen_tracked(watch: Watch, variant: Variant, show: Show) -> bool:
    """Shows whose seat categories are watched for sold out -> seats again (sold-out shows too)."""
    return bool(watch.reopen_cinemas) and _at(show, watch.reopen_cinemas) and _in_filters(watch, variant, show)


def category_watched(watch: Watch, name: str) -> bool:
    return not watch.reopen_categories or _has(name, watch.reopen_categories)


def _in_filters(watch: Watch, variant: Variant, show: Show) -> bool:
    if watch.dates and show.date_code not in watch.dates:
        return False
    if watch.formats and not _has(f"{variant.format} {show.screen_attr}", watch.formats):
        return False
    if watch.after is not None or watch.before is not None:
        try:
            t = int(show.time_code)
        except ValueError:
            return False
        if watch.after is not None and t < watch.after:
            return False
        if watch.before is not None and t > watch.before:
            return False
    return True
