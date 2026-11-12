"""BookMyShow client and response parsers.

Two sources, both what the BookMyShow website itself loads:

- the movie page for a city, whose "Book tickets" button lists every
  language/format version (each has its own event code) once booking is open;
- the showtimes API for one event code, which returns the bookable dates and
  every show (cinema, time, screen, seat categories) for one date.

Parsers are pure functions so they can be tested against saved responses.
Anything that doesn't look like a known response raises BmsError: a check
that can't be understood must never be read as "not open yet".
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests

log = logging.getLogger(__name__)

BASE = "https://in.bookmyshow.com"
SHOWTIMES_API = BASE + "/api/movies-data/v4/showtimes-by-event/primary-dynamic"
NO_SHOWS_ERROR = "1a.1b.2001"  # "There are no shows available in this region."

AVAILABILITY = {"0": "sold out", "1": "almost full", "2": "filling fast", "3": "available"}


class BmsError(Exception):
    """A request failed or returned something we don't recognise."""


@dataclass(frozen=True)
class Variant:
    language: str
    format: str
    code: str

    @property
    def label(self) -> str:
        return " ".join(p for p in (self.language, self.format) if p)


@dataclass
class MoviePage:
    code: str
    title: str
    region: str
    open: bool
    variants: list[Variant] = field(default_factory=list)


@dataclass(frozen=True)
class Category:
    name: str
    price: str
    status: str


@dataclass(frozen=True)
class Show:
    event_code: str
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    status: str
    categories: tuple[Category, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.event_code}|{self.venue_code}|{self.date_code}|{self.session_id}"

    @property
    def available(self) -> bool:
        if self.categories:
            return any(c.status not in ("0", "") for c in self.categories)
        return self.status not in ("0", "")


@dataclass
class Showtimes:
    event_code: str
    open: bool
    date_code: str = ""
    dates: list[str] = field(default_factory=list)
    label: str = ""
    shows: list[Show] = field(default_factory=list)


@dataclass(frozen=True)
class MovieRef:
    city: str
    slug: str
    code: str

    @property
    def page_url(self) -> str:
        return f"{BASE}/{self.city}/movies/{self.slug}/{self.code}"

    def booking_url(self, code: str, date_code: str) -> str:
        return f"{BASE}/movies/{self.city}/{self.slug}/buytickets/{code}/{date_code}"


def parse_movie_url(url: str) -> MovieRef:
    """Accept the movie URLs BookMyShow uses, e.g.

    https://in.bookmyshow.com/hyderabad/movies/ramayana/ET00451914
    https://in.bookmyshow.com/movies/hyderabad/ramayana/ET00451914
    https://in.bookmyshow.com/movies/hyderabad/ramayana/buytickets/ET00451914/20261112
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    code = next((p for p in parts if re.fullmatch(r"ET\d{8}", p)), None)
    if not code or "movies" not in parts:
        raise ValueError(f"not a BookMyShow movie URL: {url}")
    i = parts.index("movies")
    if i == 0 and len(parts) > 2:
        city, slug = parts[1], parts[2]
    elif i == 1:
        city, slug = parts[0], parts[2]
    else:
        raise ValueError(f"can't find the city in: {url}")
    if slug == code or slug == "buytickets":
        raise ValueError(f"can't find the movie name in: {url}")
    return MovieRef(city=city, slug=slug, code=code)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_movie_page(html: str, code: str) -> MoviePage:
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S)
    if not m:
        raise BmsError("movie page has no page state (blocked or changed?)")
    try:
        state = json.loads(m.group(1))
        queries = state["synopsisMoviesApi"]["queries"]
    except (ValueError, KeyError, TypeError) as e:
        raise BmsError(f"movie page state not understood: {e!r}") from e

    key = next((k for k in queries if k.startswith("fetchSynopsisInitDynamic") and code in k), None)
    if key is None:
        raise BmsError("movie page has no movie details")
    query = queries[key]
    data = query.get("data")
    if query.get("status") != "fulfilled" or not isinstance(data, dict):
        raise BmsError(f"movie details not loaded (status {query.get('status')})")

    region = re.search(r"-rgn-([A-Z0-9]+)-", key)
    banner = data.get("bannerWidget") or {}
    title = banner.get("heading") or (data.get("eventState") or {}).get("eventName") or code
    ctas = banner.get("pageCta")
    if not isinstance(ctas, list):
        raise BmsError("movie page has no booking button data")

    variants: list[Variant] = []
    book_button = False
    for cta in ctas:
        if "book" in _strip_tags(cta.get("text", "")).lower():
            book_button = True
        for opt in (cta.get("meta") or {}).get("options") or []:
            for fmt in opt.get("formats") or []:
                vcode = fmt.get("eventCode", "")
                if re.fullmatch(r"ET\d{8}", vcode):
                    v = Variant(_clean(opt.get("language", "")), _clean(fmt.get("dimension", "")), vcode)
                    if v not in variants:
                        variants.append(v)

    is_open = bool(variants) or book_button
    if is_open and not variants:
        variants = [Variant("", "", code)]
    return MoviePage(code=code, title=_clean(title), region=region.group(1) if region else "",
                     open=is_open, variants=variants)


def parse_showtimes(payload: dict, code: str) -> Showtimes:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise BmsError("showtimes response not understood")
    error = (payload.get("metadata") or {}).get("error")
    if error:
        if error.get("errorCode") == NO_SHOWS_ERROR:
            return Showtimes(event_code=code, open=False)
        raise BmsError(f"showtimes error {error.get('errorCode')}: {error.get('message')}")

    data = payload["data"]
    sticky = data.get("topStickyWidgets")
    widgets = data.get("showtimeWidgets")
    if not isinstance(sticky, list) or not isinstance(widgets, list):
        raise BmsError("showtimes response has no shows section")

    dates: list[str] = []
    label = ""
    for w in sticky:
        if w.get("type") == "horizontal-block-list":
            for item in w.get("data") or []:
                d = str(item.get("id", ""))
                if re.fullmatch(r"\d{8}", d) and item.get("styleId") != "date-disabled":
                    dates.append(d)
        elif w.get("type") == "horizontal-text-list":
            for item in w.get("data") or []:
                for row in (item.get("leftText") or {}).get("data") or []:
                    for c in row.get("components") or []:
                        if c.get("text"):
                            label = _clean(c["text"])

    shows: list[Show] = []
    for w in widgets:
        if w.get("type") != "groupList":
            continue
        for group in w.get("data") or []:
            for card in group.get("data") or []:
                if card.get("type") != "venue-card":
                    continue
                venue = card.get("additionalData") or {}
                for st in card.get("showtimes") or []:
                    sa = st.get("additionalData") or {}
                    cats = tuple(
                        Category(_clean(c.get("priceDesc", "")), str(c.get("curPrice", "")),
                                 str(c.get("availStatus", "")))
                        for c in sa.get("categories") or []
                    )
                    shows.append(Show(
                        event_code=code,
                        venue_code=str(venue.get("venueCode", "")),
                        venue_name=_clean(venue.get("venueName", "")),
                        session_id=str(sa.get("sessionId", "")),
                        date_code=str(sa.get("showDateCode", "")),
                        time=_clean(st.get("title") or sa.get("showTime", "")),
                        time_code=str(sa.get("showTimeCode", "")),
                        screen_attr=_clean(st.get("screenAttr") or sa.get("attributes", "")),
                        status=str(sa.get("availStatus", "")),
                        categories=cats,
                    ))

    date_code = str((data.get("additionalData") or {}).get("dateCode", ""))
    return Showtimes(event_code=code, open=True, date_code=date_code, dates=dates,
                     label=label, shows=shows)


class BmsClient:
    """Fetches like one browser would: one cookie session, Chrome's fingerprint, Indian English.

    Cookies are kept like a browser keeps them: across errors, and (with cookie_dir) across
    restarts, one set per route. With a proxy (e.g. a home connection), requests go through
    it while it works and fall back to a direct connection for a few minutes when it can't
    be reached.
    """

    PROXY_RETRY_AFTER = 300  # seconds to stay direct after the proxy fails

    def __init__(self, proxy: str | None = None, timeout: float = 20, cookie_dir: Path | None = None):
        self.proxy = proxy
        self.timeout = timeout
        self.cookie_dir = cookie_dir
        self._sessions: dict[bool, requests.Session] = {}
        self._proxy_down_until = 0.0
        self.last_route = "direct"

    def _cookie_file(self, proxied: bool) -> Path | None:
        if self.cookie_dir is None:
            return None
        return self.cookie_dir / f"cookies-{'proxy' if proxied else 'direct'}.json"

    def _session(self, proxied: bool) -> requests.Session:
        if proxied not in self._sessions:
            session = requests.Session(
                impersonate="chrome", proxy=self.proxy if proxied else None,
                headers={"Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7"})
            load_cookies(session, self._cookie_file(proxied))
            self._sessions[proxied] = session
        return self._sessions[proxied]

    def _get(self, url: str, **kw):
        if self.proxy and time.monotonic() >= self._proxy_down_until:
            session = self._session(True)
            try:
                resp = session.get(url, timeout=self.timeout, **kw)
                self.last_route = "proxy"
                return self._check(resp, url, session, proxied=True)
            except requests.RequestsError as e:
                log.warning("proxy unreachable (%s), going direct for %d s", e.__class__.__name__,
                            self.PROXY_RETRY_AFTER)
                self._proxy_down_until = time.monotonic() + self.PROXY_RETRY_AFTER
        session = self._session(False)
        try:
            resp = session.get(url, timeout=self.timeout, **kw)
        except requests.RequestsError as e:
            raise BmsError(f"request failed: {e}") from e
        self.last_route = "direct"
        return self._check(resp, url, session, proxied=False)

    def _check(self, resp, url: str, session: requests.Session, proxied: bool):
        save_cookies(session, self._cookie_file(proxied))
        if resp.status_code != 200:
            route = "proxy" if proxied else "direct"
            mitigated = resp.headers.get("cf-mitigated")
            raise BmsError(f"HTTP {resp.status_code} from {urlparse(url).path} ({route}"
                           f"{', cloudflare ' + mitigated if mitigated else ''})")
        return resp

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()

    def movie_page(self, ref: MovieRef) -> MoviePage:
        return parse_movie_page(self._get(ref.page_url).text, ref.code)

    def showtimes(self, region: str, code: str, date_code: str = "", referer: str = "") -> Showtimes:
        params = {"eventCode": code, "dateCode": date_code, "isDesktop": "true",
                  "regionCode": region, "xLocationShared": "false",
                  "memberId": "", "lsId": "", "subCode": ""}
        headers = {"x-app-code": "WEB", "x-region-code": region}
        if referer:
            headers["Referer"] = referer
        resp = self._get(SHOWTIMES_API, params=params, headers=headers)
        try:
            payload = resp.json()
        except ValueError as e:
            raise BmsError("showtimes response is not JSON (blocked?)") from e
        return parse_showtimes(payload, code)


def save_cookies(session: requests.Session, path: Path | None) -> None:
    if path is None:
        return
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                "expires": c.expires, "secure": c.secure} for c in session.cookies.jar]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cookies))
    tmp.replace(path)


def load_cookies(session: requests.Session, path: Path | None) -> None:
    if path is None or not path.exists():
        return
    try:
        cookies = json.loads(path.read_text())
    except ValueError:
        return
    now = time.time()
    for c in cookies:
        if c.get("expires") and c["expires"] < now:
            continue
        session.cookies.set(c["name"], c["value"], domain=c.get("domain") or "", path=c.get("path") or "/",
                            secure=bool(c.get("secure")))
