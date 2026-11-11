"""The check loop: look at each watch, message about anything new."""

from __future__ import annotations

import html
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .bms import BmsClient, BmsError, MoviePage, Show, Variant
from .config import Settings, Watch, load_watches
from .matcher import show_matches, variants_to_check
from .store import Store

log = logging.getLogger(__name__)
MAX_DATES = 3  # dates checked per version when a watch names none
STATUS_ICON = {"3": "🟢", "2": "🟠", "1": "🟡", "0": "🔴"}


@dataclass
class Result:
    watch: Watch
    page: MoviePage
    open: bool = False  # a version this watch covers has shows in the city
    open_versions: list[Variant] = field(default_factory=list)
    matches: list[tuple[Variant, Show]] = field(default_factory=list)


def _variant_from_label(code: str, label: str) -> Variant:
    language, _, fmt = label.partition("•")
    return Variant(language.strip(), fmt.strip(), code)


def check_watch(client: BmsClient, watch: Watch, pause: Callable[[], None] = lambda: None) -> Result:
    """Two independent "open" signals, so a page layout we haven't seen can't hide an opening:
    the movie page's Book tickets versions, and the showtimes API for the watched code itself.
    """
    page = client.movie_page(watch.movie)
    result = Result(watch, page)
    if not page.region:
        raise BmsError("movie page has no region code")
    referer = watch.movie.page_url
    variants = page.variants
    prefetched = {}
    if not page.open:
        pause()
        own = client.showtimes(page.region, watch.movie.code, referer=referer)
        if not own.open:
            return result
        variants = [_variant_from_label(watch.movie.code, own.label)]
        prefetched[watch.movie.code] = own

    for variant in variants_to_check(watch, variants):
        if variant.code in prefetched:
            first = prefetched[variant.code]
        else:
            pause()
            first = client.showtimes(page.region, variant.code, referer=referer)
        if not first.open:
            continue  # listed version without shows in this city yet
        result.open = True
        result.open_versions.append(variant)
        wanted = [d for d in first.dates if not watch.dates or d in watch.dates]
        if not watch.dates:
            wanted = wanted[:MAX_DATES]
        batches = [first]
        for d in wanted:
            if d != first.date_code:
                pause()
                batches.append(client.showtimes(page.region, variant.code, d, referer=referer))
        seen = set()
        for batch in batches:
            for show in batch.shows:
                if show.key in seen or show.date_code not in wanted:
                    continue
                seen.add(show.key)
                if show_matches(watch, variant, show):
                    result.matches.append((variant, show))
    return result


def _day(date_code: str) -> str:
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a %d %b")
    except ValueError:
        return date_code


def _icon(show: Show) -> str:
    statuses = [c.status for c in show.categories] or [show.status]
    return STATUS_ICON.get(max(statuses), "")


def opened_message(r: Result) -> str:
    w, e = r.watch, html.escape
    versions = ", ".join(e(v.label) for v in r.open_versions if v.label) or "the one you watch"
    lines = [f"🎬 <b>{e(r.page.title)}</b>: booking is open in {e(w.movie.city.title())}",
             f"Versions: {versions}"]
    if not r.matches:
        lines.append("No shows match your filters yet. I'll message you as soon as one does.")
    lines.append(f'<a href="{e(w.movie.page_url)}">Open on BookMyShow</a>')
    return "\n".join(lines)


def shows_message(r: Result, new: list[tuple[Variant, Show]]) -> str:
    e = html.escape
    count = len(new)
    out = [f"🎟 <b>{e(r.page.title)}</b>: {count} new show{'s' if count != 1 else ''} matching your filters"]
    groups: dict[tuple[str, str, str], dict[str, list[Show]]] = {}
    for variant, show in sorted(new, key=lambda m: (m[1].date_code, m[0].label, m[1].venue_name, m[1].time_code)):
        groups.setdefault((show.date_code, variant.label, variant.code), {}).setdefault(show.venue_name, []).append(show)
    for (date_code, label, code), venues in groups.items():
        block = [f"<b>{e(_day(date_code))}{' · ' + e(label) if label else ''}</b>"]
        for venue, shows in venues.items():
            times = ", ".join(
                f"{e(s.time)}{' (' + e(s.screen_attr) + ')' if s.screen_attr else ''} {_icon(s)}".strip()
                for s in shows)
            block.append(f"{e(venue)}: {times}")
        block.append(f'<a href="{e(r.watch.movie.booking_url(code, date_code))}">Book</a>')
        out.append("\n".join(block))
    return "\n\n".join(out)


class Monitor:
    def __init__(self, settings: Settings, client: BmsClient, store: Store, send: Callable[[str], bool]):
        self.settings = settings
        self.client = client
        self.store = store
        self.send = send
        self._last_scan: dict[str, float] = {}

    def due(self, watch: Watch) -> bool:
        """Every tick until booking opens; after the "open" alert, at most every open_interval.
        The opening is the time-critical part; later scans of every version and date cost the
        most requests, so they run less often."""
        last = self._last_scan.get(watch.key)
        if last is not None and self.store.was_opened(watch.key):
            return time.monotonic() - last >= self.settings.open_interval * 0.9
        return True

    def pause(self) -> None:
        time.sleep(random.uniform(1.0, 3.0))

    def tick(self) -> list[Result]:
        try:
            watches = [w for w in load_watches(self.settings.watches_file) if w.enabled]
        except Exception as e:
            self._failed(f"watches file: {e}")
            return []
        results, errors = [], []
        for watch in watches:
            if not self.due(watch):
                continue
            self._last_scan[watch.key] = time.monotonic()
            try:
                r = check_watch(self.client, watch, self.pause)
            except BmsError as e:
                log.warning("%s: %s", watch.name, e)
                errors.append(f"{watch.name}: {e}")
                continue
            log.info("%s: %s, %d matching show(s)", watch.name,
                     "open" if r.open else "not open", len(r.matches))
            self.deliver(r)
            results.append(r)
            self.pause()
        if errors:
            self._failed("; ".join(errors))
        else:
            self._succeeded()
        return results

    def deliver(self, r: Result) -> None:
        key = r.watch.key
        if r.open and not self.store.was_opened(key):
            if self.send(opened_message(r)):
                self.store.mark_opened(key)
        if not r.matches:
            return
        done = self.store.notified(key, [s.key for _, s in r.matches])
        new = [(v, s) for v, s in r.matches if s.key not in done]
        if new and self.send(shows_message(r, new)):
            self.store.mark_notified(key, [s.key for _, s in new])

    def _failed(self, error: str) -> None:
        failures = self.store.record_failure(error)
        _, since, _, alerted = self.store.health()
        if failures >= self.settings.fail_alert_after and not alerted:
            mins = max(1, round((time.time() - (since or time.time())) / 60))
            msg = (f"⚠️ BookMyShow checks have failed {failures} times in a row (~{mins} min). "
                   f"Alerts can't be trusted until this clears.\n<code>{html.escape(error[:500])}</code>")
            if self.send(msg):
                self.store.set_alerted(True)

    def _succeeded(self) -> None:
        _, _, _, alerted = self.store.health()
        if alerted and self.send("✅ BookMyShow checks are working again."):
            self.store.set_alerted(False)
        self.store.record_success()

    def heartbeat(self, now: time.struct_time | None = None) -> None:
        """Once a day, after the configured time, say we're alive (silence would look like "not open")."""
        at = self.settings.heartbeat_at
        now = now or time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if at is None or now.tm_hour * 100 + now.tm_min < at or self.store.get("heartbeat") == today:
            return
        try:
            names = [w.name for w in load_watches(self.settings.watches_file) if w.enabled]
        except Exception:
            names = []
        failures, _, error, _ = self.store.health()
        state = "last check OK" if failures == 0 else f"last {failures} checks failed: {error}"
        msg = f"👀 Still watching {len(names)} movie{'s' if len(names) != 1 else ''} ({state})."
        if names:
            msg += "\n" + "\n".join(f"• {html.escape(n)}" for n in names)
        if self.send(msg):
            self.store.put("heartbeat", today)

    def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self.tick()
                self.heartbeat()
            except Exception:
                log.exception("check failed unexpectedly")
                self._failed("unexpected error, see logs")
            elapsed = time.monotonic() - started
            time.sleep(max(5.0, self.next_delay() - elapsed))

    def next_delay(self) -> float:
        """About one interval, randomised. After failures, back off (2x, 4x... up to 5 min)
        rather than retrying at full speed, which is what turns a hiccup into a block."""
        failures = self.store.health()[0]
        base = self.settings.interval * (2 ** min(failures, 4) if failures else 1)
        return min(max(300.0, self.settings.interval), base) * random.uniform(0.85, 1.15)
