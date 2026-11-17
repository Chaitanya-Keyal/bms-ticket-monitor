"""The check loop: look at each watch, message about anything new."""

from __future__ import annotations

import html
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .bms import AVAILABILITY, BmsClient, BmsError, Category, MoviePage, Show, Variant
from .config import Settings, Watch, load_watches
from .matcher import at_priority_cinema, category_watched, show_matches, show_reopen_tracked, variants_to_check
from .store import Store

log = logging.getLogger(__name__)
MAX_DATES = 3  # dates checked per version when a watch names none
# BMS order, fewest seats first: sold out, almost full, filling fast, available
# (BMS paints both middle states orange; these icons read in order instead)
STATUS_ICON = {"3": "🟢", "2": "🟡", "1": "🟠", "0": "🔴"}


@dataclass
class Result:
    watch: Watch
    page: MoviePage
    open: bool = False  # a version this watch covers has shows in the city
    activated: bool = False  # BMS switched it on in the city, possibly before listing shows
    open_versions: list[Variant] = field(default_factory=list)
    matches: list[tuple[Variant, Show]] = field(default_factory=list)
    tracked: list[tuple[Variant, Show]] = field(default_factory=list)  # seat-release watch


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
        result.activated = own.activated
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
        result.activated = result.activated or first.activated
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
                if show_reopen_tracked(watch, variant, show):
                    result.tracked.append((variant, show))
    return result


def _day(date_code: str) -> str:
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a %d %b")
    except ValueError:
        return date_code


def _icon(show: Show) -> str:
    statuses = [c.status for c in show.categories] or [show.status]
    return STATUS_ICON.get(max(statuses), "")


def activated_message(r: Result) -> str:
    e = html.escape
    return (f"⏳ <b>{e(r.page.title)}</b>: BookMyShow just switched it on in {e(r.watch.movie.city.title())}, "
            f"but no shows are listed yet. Booking looks about to open; checking every minute.\n"
            f'<a href="{e(r.watch.movie.page_url)}">Open on BookMyShow</a>')


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


def reopen_message(r: Result, events: list[tuple[Variant, Show, Category, str]]) -> str:
    e = html.escape
    venues = sorted({s.venue_name for _, s, _, _ in events})
    out = [f"🔓 <b>{e(r.page.title)}</b>: more seats free at {e(', '.join(venues))}"]
    groups: dict[tuple[str, str, str], list[str]] = {}
    for v, s, c, prev in sorted(events, key=lambda x: (x[1].date_code, x[0].label, x[1].venue_name, x[1].time_code)):
        line = (f"{e(s.venue_name)}: {e(s.time)}: {e(c.name)}"
                f"{' ₹' + e(c.price.split('.')[0]) if c.price else ''}: "
                f"{e(AVAILABILITY.get(prev, prev))} {STATUS_ICON.get(prev, '')} → "
                f"{e(AVAILABILITY.get(c.status, c.status))} {STATUS_ICON.get(c.status, '')}".strip())
        groups.setdefault((s.date_code, v.label, v.code), []).append(line)
    for (date_code, label, code), lines in groups.items():
        out.append("\n".join([f"<b>{e(_day(date_code))}{' · ' + e(label) if label else ''}</b>", *lines,
                              f'<a href="{e(r.watch.movie.booking_url(code, date_code))}">Book</a>']))
    return "\n\n".join(out)


class Monitor:
    def __init__(self, settings: Settings, client: BmsClient, store: Store, send: Callable[[str], bool],
                 call: Callable[..., bool] | None = None):
        self.settings = settings
        self.call = call
        self.client = client
        self.store = store
        self.send = send
        self._last_scan: dict[str, float] = {}
        self._open: dict[str, bool] = {}

    def due(self, watch: Watch) -> bool:
        """Every tick until booking opens and for open_fast_for seconds after (when cinemas and the
        best seats appear), then at most every open_interval, since full scans of every version
        and date cost the most requests."""
        last = self._last_scan.get(watch.key)
        opened_at = self.store.opened_at(watch.key)
        if last is None or opened_at is None or time.time() - opened_at < self.settings.open_fast_for:
            return True
        return time.monotonic() - last >= self.settings.open_interval * 0.9

    def pause(self) -> None:
        time.sleep(random.uniform(1.0, 3.0))

    def tick(self) -> list[Result]:
        try:
            return self._tick()
        finally:
            self.write_status()

    def write_status(self) -> None:
        """A small JSON file next to the state for outside health checks (read-only for them)."""
        failures, _, error, _ = self.store.health()
        last_good = self.store.get("last_good")
        status = {
            "updated": int(time.time()),
            "last_good": int(last_good) if last_good else None,
            "failures": failures,
            "last_error": error,
            "route": getattr(self.client, "last_route", "direct"),
            "direct_blocked": bool(getattr(self.client, "direct_blocked", False)),
            "watches": [{"name": n, "open": o} for n, o in self._open.items()],
        }
        path = self.settings.state_db.parent / "status.json"
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(status))
            tmp.replace(path)
        except OSError as e:
            log.warning("can't write %s: %s", path, e)

    def _tick(self) -> list[Result]:
        try:
            watches = [w for w in load_watches(self.settings.watches_file) if w.enabled]
        except Exception as e:
            self._failed(f"watches file: {e}")
            return []
        self._open = {w.name: self._open.get(w.name, False) for w in watches}
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
                     "open" if r.open else ("activated, no shows yet" if r.activated else "not open"),
                     len(r.matches))
            self._open[watch.name] = r.open
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
        if (r.activated and not r.open and not self.store.was_opened(key)
                and not self.store.get(f"activated:{key}")):
            if self.send(activated_message(r)):
                self.store.put(f"activated:{key}", str(int(time.time())))
        if r.open and not self.store.was_opened(key):
            if self.send(opened_message(r)):
                self.store.mark_opened(key)
        self._called_now: set[str] = set()  # labels rung for this movie in this check
        who = r.watch.call
        attempted = (r.open or r.activated) and self.ring(key, opening=True, who=who)
        done = self.store.notified(key, [s.key for _, s in r.matches]) if r.matches else set()
        new = [(v, s) for v, s in r.matches if s.key not in done]
        if new and self.send(shows_message(r, new)):
            self.store.mark_notified(key, [s.key for _, s in new])
            if r.watch.priority_call and any(at_priority_cinema(r.watch, s) for _, s in new):
                attempted = self.ring(key, opening=False, who=r.watch.priority_call, priority=True) or attempted
            elif not attempted:  # at most one ordinary call attempt per movie per check
                attempted = self.ring(key, opening=False, who=who)
        if r.tracked and self.deliver_reopened(r) and not attempted:
            self.ring(key, opening=False, who=r.watch.reopen_call if r.watch.reopen_call is not None else who)

    REOPEN_COOLDOWN = 600  # a category that keeps flipping alerts at most every 10 minutes

    def deliver_reopened(self, r: Result) -> bool:
        """Seat-release alerts for steps that matter: sold out -> any seats, or almost full /
        filling fast -> available. (almost full -> filling fast is orange -> orange on BMS, so it
        isn't alerted.) The first sighting of a category only records it. Returns True if sent."""
        key, now = r.watch.key, int(time.time())
        events, seen = [], []
        for v, s in r.tracked:
            for c in s.categories:
                if not category_watched(r.watch, c.name):
                    continue
                prev, alerted = self.store.seat(key, s.key, c.name)
                better = (prev == "0" and c.status in ("1", "2", "3")) or (prev in ("1", "2") and c.status == "3")
                if better and now - (alerted or 0) >= self.REOPEN_COOLDOWN:
                    events.append((v, s, c, prev))
                else:
                    seen.append((s.key, c.name, c.status, alerted))
        for show, cat, status, alerted in seen:
            self.store.put_seat(key, show, cat, status, alerted)
        if not events:
            return False
        if not self.send(reopen_message(r, events)):
            return False  # statuses left as sold out, so the next check tries again
        for _, s, c, _ in events:
            self.store.put_seat(key, s.key, c.name, c.status, now)
        return True

    CALL_WINDOW = 1800  # opening calls only within 30 min of it; later it's old news

    PRIORITY_GAP = 120  # priority calls skip call_min_gap but don't re-ring within 2 minutes

    def ring(self, key: str, opening: bool, who: list[str] | None = None, priority: bool = False) -> bool:
        """Ring phones: once at a movie's first heads-up or opening (up to 3 tries if Twilio
        fails), and after new-shows or seat alerts, never twice about one movie within
        call_min_gap. A priority call (new show at a priority cinema) uses PRIORITY_GAP instead.
        `who` limits it to those labels (None = everyone, [] = nobody); nobody is rung twice in
        one check. Returns True if a call was attempted."""
        if self.call is None or who == []:
            return False
        called = getattr(self, "_called_now", set())
        if "*" in called:
            return False
        targets = None if who is None else [w for w in who if w not in called]
        if targets == []:
            return False
        now = time.time()
        gap_key, gap = (f"last-priority-call:{key}", self.PRIORITY_GAP) if priority else \
            (f"last-call:{key}", self.settings.call_min_gap)
        if now - float(self.store.get(gap_key) or 0) < gap:
            return False
        if opening:
            if self.store.get(f"called:{key}"):
                return False
            seen = [int(t) for t in (self.store.opened_at(key), self.store.get(f"activated:{key}")) if t]
            if not seen or now - min(seen) > self.CALL_WINDOW:
                return False
            tries = int(self.store.get(f"call-tries:{key}") or 0)
            if tries >= 3:
                return False
            self.store.put(f"call-tries:{key}", str(tries + 1))
        called.update(targets if targets is not None else ["*"])
        if self.call("", only=targets):
            self.store.put(gap_key, str(int(now)))
            if opening:
                self.store.put(f"called:{key}", str(int(now)))
        return True

    def _failed(self, error: str) -> None:
        """fail_alert_after <= 0 turns the failure alert off (an outside health check owns it)."""
        failures = self.store.record_failure(error)
        _, since, _, alerted = self.store.health()
        if 0 < self.settings.fail_alert_after <= failures and not alerted:
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
        self.store.put("last_good", str(int(time.time())))

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
                self.write_status()
            elapsed = time.monotonic() - started
            time.sleep(max(5.0, self.next_delay() - elapsed))

    def next_delay(self) -> float:
        """About one interval, randomised. After failures, back off (2x, 4x... up to 5 min)
        rather than retrying at full speed, which is what turns a hiccup into a block."""
        failures = self.store.health()[0]
        base = self.settings.interval * (2 ** min(failures, 4) if failures else 1)
        return min(max(300.0, self.settings.interval), base) * random.uniform(0.85, 1.15)
