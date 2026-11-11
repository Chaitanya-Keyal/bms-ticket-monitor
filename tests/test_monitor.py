import json
from pathlib import Path

import pytest

from bms_monitor.bms import (BmsError, MovieRef, Variant, parse_movie_page, parse_movie_url,
                             parse_showtimes)
from bms_monitor.config import Watch, load_watches
from bms_monitor.matcher import show_matches, variants_to_check
from bms_monitor.monitor import Monitor, check_watch, shows_message
from bms_monitor.store import Store
from bms_monitor.telegram import split

FIX = Path(__file__).parent / "fixtures"
MOVIE = MovieRef("hyderabad", "ramayana", "ET00451914")


def load(name):
    p = FIX / name
    return json.loads(p.read_text()) if p.suffix == ".json" else p.read_text()


# --- parsing -----------------------------------------------------------------

def test_parse_movie_url_forms():
    for url in ["https://in.bookmyshow.com/hyderabad/movies/ramayana/ET00451914",
                "https://in.bookmyshow.com/movies/hyderabad/ramayana/ET00451914",
                "https://in.bookmyshow.com/movies/hyderabad/ramayana/buytickets/ET00451914/20261112"]:
        assert parse_movie_url(url) == MOVIE
    with pytest.raises(ValueError):
        parse_movie_url("https://in.bookmyshow.com/explore/movies-hyderabad")


def test_movie_page_not_open():
    page = parse_movie_page(load("page-notopen.html"), "ET00521922")
    assert page.title == "Talcum Powder"
    assert page.region == "HYD"
    assert page.open is False and page.variants == []


def test_movie_page_open_lists_versions():
    page = parse_movie_page(load("page-open.html"), "ET00451914")
    assert page.open and page.region == "HYD"
    assert Variant("Hindi", "4DX 3D", "ET00523224") in page.variants
    assert Variant("Telugu", "2D", "ET00518312") in page.variants
    assert all(v.code != "*" for v in page.variants)


def test_movie_page_garbage_is_an_error_not_closed():
    with pytest.raises(BmsError):
        parse_movie_page("<html><title>Attention Required! | Cloudflare</title></html>", "ET00451914")


def test_showtimes_not_open():
    st = parse_showtimes(load("showtimes-notopen.json"), "ET00521922")
    assert st.open is False and st.shows == []


def test_showtimes_open():
    st = parse_showtimes(load("showtimes-open.json"), "ET00514068")
    assert st.open and st.date_code == "20261112"
    assert st.dates[0] == "20261112" and len(st.dates) == 8
    assert st.label == "Telugu • 2D"
    show = next(s for s in st.shows if s.venue_code == "ALUC" and s.session_id == "6943")
    assert show.venue_name == "ALLU Cinemas: Kokapet"
    assert show.time == "07:45 PM" and show.time_code == "1945"
    assert show.screen_attr == "LASER 4K ATMOS"
    assert show.available  # GOLD is almost full, PLATINUM sold out


def test_showtimes_unknown_shapes_are_errors():
    with pytest.raises(BmsError):
        parse_showtimes({"data": {}}, "ET1")
    with pytest.raises(BmsError):
        parse_showtimes({"data": {}, "metadata": {"error": {"errorCode": "9.9", "message": "x"}}}, "ET1")
    with pytest.raises(BmsError):
        parse_showtimes(["not", "a", "dict"], "ET1")


# --- matching ----------------------------------------------------------------

def watch(**kw):
    return Watch(name="t", movie=MOVIE, **kw)


def test_variants_formats_narrow_only_when_they_are_versions():
    vs = parse_movie_page(load("page-open.html"), "ET00451914").variants
    picked = variants_to_check(watch(languages=["Hindi"], formats=["4DX"]), vs)
    assert [v.code for v in picked] == ["ET00523224"]
    # LASER is a screen type, not a version: keep every Hindi version
    picked = variants_to_check(watch(languages=["Hindi"], formats=["LASER"]), vs)
    assert {v.language for v in picked} == {"Hindi"} and len(picked) == 4


def test_show_filters():
    st = parse_showtimes(load("showtimes-open.json"), "ET00514068")
    v = Variant("Telugu", "2D", "ET00514068")
    show = next(s for s in st.shows if s.venue_code == "ALUC" and s.session_id == "6943")
    assert show_matches(watch(), v, show)
    assert show_matches(watch(cinemas=["allu"]), v, show)
    assert show_matches(watch(cinemas=["ALUC"]), v, show)
    assert not show_matches(watch(cinemas=["Prasads"]), v, show)
    assert show_matches(watch(formats=["laser"]), v, show)
    assert not show_matches(watch(formats=["IMAX"]), v, show)
    assert show_matches(watch(dates=["20261112"], after=1800, before=2100), v, show)
    assert not show_matches(watch(dates=["20261113"]), v, show)
    assert not show_matches(watch(after=2000), v, show)


# --- the check flow ----------------------------------------------------------

class FakeClient:
    def __init__(self, page, showtimes):
        self.page, self.st, self.calls = page, showtimes, []

    def movie_page(self, ref):
        self.calls.append(("page", ref.code))
        return parse_movie_page(load(self.page), ref.code)

    def showtimes(self, region, code, date_code="", referer=""):
        self.calls.append(("showtimes", code, date_code))
        return parse_showtimes(load(self.st), code)


def test_not_open_costs_a_page_and_one_small_api_call():
    client = FakeClient("page-notopen.html", "showtimes-notopen.json")
    r = check_watch(client, Watch(name="t", movie=MovieRef("hyderabad", "talcum-powder", "ET00521922")))
    assert not r.open and r.matches == []
    assert client.calls == [("page", "ET00521922"), ("showtimes", "ET00521922", "")]


def test_api_shows_open_even_when_page_has_no_button():
    client = FakeClient("page-notopen.html", "showtimes-open.json")
    r = check_watch(client, Watch(name="t", movie=MovieRef("hyderabad", "talcum-powder", "ET00521922")))
    assert r.open and r.matches
    assert r.open_versions == [Variant("Telugu", "2D", "ET00521922")]
    assert client.calls.count(("showtimes", "ET00521922", "")) == 1  # reused, not fetched twice


def test_listed_versions_without_shows_are_not_open():
    client = FakeClient("page-open.html", "showtimes-notopen.json")
    r = check_watch(client, watch(languages=["Telugu"]))
    assert r.page.open and not r.open and r.open_versions == []


def test_open_finds_matches_and_trusts_show_dates():
    # the fake returns 12 Nov for every date asked, like BMS does for a missing date
    client = FakeClient("page-open.html", "showtimes-open.json")
    r = check_watch(client, watch(languages=["Telugu"], formats=["2D"], dates=["20261112", "20261113"]))
    assert r.page.open and r.matches
    assert all(s.date_code == "20261112" for _, s in r.matches)


def test_monitor_sends_once_and_only_marks_after_success(tmp_path):
    wf = tmp_path / "watches.toml"
    wf.write_text('[[watch]]\nname = "R"\nurl = "https://in.bookmyshow.com/hyderabad/movies/ramayana/ET00451914"\n'
                  'languages = ["Telugu"]\nformats = ["2D"]\n')
    from bms_monitor.config import Settings
    settings = Settings(wf, tmp_path / "s.db", 60, 3, None, None, "t", "c", None)
    sent, ok = [], [False]

    def send(msg):
        sent.append(msg)
        return ok[0]

    m = Monitor(settings, FakeClient("page-open.html", "showtimes-open.json"), Store(settings.state_db), send)
    m.pause = lambda: None
    m.tick()
    assert len(sent) == 2  # opened + shows, both "failed" to send
    ok[0] = True
    m.tick()
    assert len(sent) == 4 and "booking is open" in sent[2] and "new show" in sent[3]
    m.tick()
    assert len(sent) == 4  # nothing new


def test_failure_alert_after_threshold(tmp_path):
    from bms_monitor.config import Settings
    wf = tmp_path / "watches.toml"
    wf.write_text('[[watch]]\nurl = "https://in.bookmyshow.com/hyderabad/movies/ramayana/ET00451914"\n')
    settings = Settings(wf, tmp_path / "s.db", 60, 3, None, None, "t", "c", None)

    class Broken:
        def movie_page(self, ref):
            raise BmsError("HTTP 403")

    sent = []
    m = Monitor(settings, Broken(), Store(settings.state_db), lambda msg: sent.append(msg) or True)
    m.pause = lambda: None
    for _ in range(5):
        m.tick()
    assert len(sent) == 1 and "failed 3 times" in sent[0]
    m.client = FakeClient("page-open.html", "showtimes-notopen.json")
    m.tick()
    assert len(sent) == 2 and "working again" in sent[1]


def test_watches_file_parsing(tmp_path):
    wf = tmp_path / "w.toml"
    wf.write_text('[[watch]]\nname = "Dune"\nurl = "https://in.bookmyshow.com/hyderabad/movies/x/ET00000001"\n'
                  'formats = "IMAX"\ndates = ["2026-12-18"]\nafter = "18:00"\n')
    (w,) = load_watches(wf)
    assert w.formats == ["IMAX"] and w.dates == ["20261218"] and w.after == 1800
    wf.write_text('[[watch]]\nurl = "https://in.bookmyshow.com/hyderabad/movies/x/ET00000001"\nafter = "6pm"\n')
    with pytest.raises(ValueError):
        load_watches(wf)


def test_long_messages_split_cleanly():
    r_text = "\n\n".join(f"<b>block {i}</b>\n" + "x" * 300 for i in range(40))
    parts = split(r_text)
    assert all(len(p) <= 4000 for p in parts) and "".join(parts).count("<b>") == 40


def test_shows_message_groups_by_day_and_cinema():
    st = parse_showtimes(load("showtimes-open.json"), "ET00514068")
    v = Variant("Telugu", "2D", "ET00514068")
    from bms_monitor.monitor import Result
    from bms_monitor.bms import MoviePage
    r = Result(watch(), MoviePage("ET00514068", "Itllu Arjuna", "HYD", True, [v]),
               matches=[(v, s) for s in st.shows])
    msg = shows_message(r, r.matches)
    assert "Thu 12 Nov · Telugu 2D" in msg and "ALLU Cinemas: Kokapet:" in msg
    assert "/buytickets/ET00514068/20261112" in msg


def test_daily_heartbeat_once_after_time(tmp_path):
    import time as t
    from bms_monitor.config import Settings
    wf = tmp_path / "watches.toml"
    wf.write_text('[[watch]]\nname = "Dune"\nurl = "https://in.bookmyshow.com/hyderabad/movies/x/ET00000001"\n')
    settings = Settings(wf, tmp_path / "s.db", 60, 3, None, 1000, "t", "c", None)
    sent = []
    m = Monitor(settings, None, Store(settings.state_db), lambda msg: sent.append(msg) or True)
    m.heartbeat(t.strptime("2026-12-01 09:59", "%Y-%m-%d %H:%M"))
    assert sent == []
    m.heartbeat(t.strptime("2026-12-01 10:00", "%Y-%m-%d %H:%M"))
    m.heartbeat(t.strptime("2026-12-01 18:00", "%Y-%m-%d %H:%M"))
    assert len(sent) == 1 and "Still watching 1 movie" in sent[0] and "Dune" in sent[0]
    m.heartbeat(t.strptime("2026-12-02 10:01", "%Y-%m-%d %H:%M"))
    assert len(sent) == 2


def test_backoff_after_failures(tmp_path):
    from bms_monitor.config import Settings
    settings = Settings(tmp_path / "w.toml", tmp_path / "s.db", 60, 3, None, None, "t", "c", None)
    store = Store(settings.state_db)
    m = Monitor(settings, None, store, lambda msg: True)
    assert 51 <= m.next_delay() <= 69
    store.record_failure("x")
    assert 102 <= m.next_delay() <= 138
    for _ in range(5):
        store.record_failure("x")
    assert 255 <= m.next_delay() <= 345  # capped at 5 min
    store.record_success()
    assert 51 <= m.next_delay() <= 69


def test_proxy_falls_back_to_direct_and_retries_later(monkeypatch):
    from curl_cffi import requests as cr
    from bms_monitor.bms import BmsClient

    class Resp:
        status_code = 200
        text = "ok"

    calls = []

    class FakeSession:
        def __init__(self, proxied, fail):
            self.proxied, self.fail = proxied, fail

        def get(self, url, **kw):
            calls.append("proxy" if self.proxied else "direct")
            if self.fail:
                raise cr.RequestsError("proxy down")
            return Resp()

        def close(self):
            pass

    proxy_ok = [False]
    client = BmsClient(proxy="socks5h://home:1080")
    monkeypatch.setattr(client, "_session", lambda proxied: FakeSession(proxied, proxied and not proxy_ok[0]))
    client._get("https://example/a")
    assert calls == ["proxy", "direct"] and client.last_route == "direct"
    client._get("https://example/b")
    assert calls[-1] == "direct" and calls.count("proxy") == 1  # not retried for a while
    proxy_ok[0] = True
    client._proxy_down_until = 0
    client._get("https://example/c")
    assert calls[-1] == "proxy" and client.last_route == "proxy"


def test_open_watches_are_scanned_less_often(tmp_path):
    from bms_monitor.config import Settings
    wf = tmp_path / "watches.toml"
    wf.write_text('[[watch]]\nurl = "https://in.bookmyshow.com/hyderabad/movies/ramayana/ET00451914"\n'
                  'languages = ["Telugu"]\nformats = ["2D"]\n')
    settings = Settings(wf, tmp_path / "s.db", 60, 3, None, None, "t", "c", None, open_interval=120)
    client = FakeClient("page-open.html", "showtimes-open.json")
    m = Monitor(settings, client, Store(settings.state_db), lambda msg: True)
    m.pause = lambda: None
    m.tick()
    first = len(client.calls)
    m.tick()  # opened: not due again yet
    assert len(client.calls) == first
    m._last_scan = {k: v - 200 for k, v in m._last_scan.items()}
    m.tick()
    assert len(client.calls) == 2 * first
