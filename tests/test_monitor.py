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


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.headers = {"cf-mitigated": "challenge"} if status == 403 else {}
        self.text = "ok"


def _routes(monkeypatch, client, direct, backup):
    """direct/backup: lists, each call pops one outcome: an HTTP status or 'down'."""
    from curl_cffi import requests as cr
    calls = []

    class S:
        def __init__(self, proxied):
            self.proxied = proxied
            self.cookies = type("C", (), {"jar": []})()

        def get(self, url, **kw):
            calls.append("backup" if self.proxied else "direct")
            outcome = (backup if self.proxied else direct).pop(0)
            if outcome == "down":
                raise cr.RequestsError("unreachable")
            return _Resp(outcome)

    monkeypatch.setattr(client, "_session", lambda proxied: S(proxied))
    return calls


def test_direct_is_the_main_route(monkeypatch):
    from bms_monitor.bms import BmsClient
    client = BmsClient(proxy="socks5h://home:1080")
    calls = _routes(monkeypatch, client, direct=[200, 200], backup=[])
    client._get("https://x/a")
    client._get("https://x/b")
    assert calls == ["direct", "direct"] and client.last_route == "direct"


def test_blocked_direct_retries_through_backup_and_stays_there(monkeypatch):
    from bms_monitor.bms import BmsClient
    client = BmsClient(proxy="socks5h://home:1080")
    calls = _routes(monkeypatch, client, direct=[403, 200], backup=[200, 200])
    client._get("https://x/a")
    assert calls == ["direct", "backup"] and client.last_route == "backup"
    client._get("https://x/b")
    assert calls[-1] == "backup" and calls.count("direct") == 1  # no hammering a blocked route
    client._direct_blocked_until = 0  # 30 minutes later
    client._get("https://x/c")
    assert calls[-1] == "direct"


def test_blocked_with_backup_unreachable_is_an_error(monkeypatch):
    from bms_monitor.bms import BmsClient, Blocked
    client = BmsClient(proxy="socks5h://home:1080")
    _routes(monkeypatch, client, direct=[403], backup=["down"])
    with pytest.raises(BmsError, match="backup route is unreachable"):
        client._get("https://x/a")
    no_proxy = BmsClient()
    _routes(monkeypatch, no_proxy, direct=[403], backup=[])
    with pytest.raises(Blocked):
        no_proxy._get("https://x/a")


def test_backup_going_away_falls_back_to_direct(monkeypatch):
    from bms_monitor.bms import BmsClient
    client = BmsClient(proxy="socks5h://home:1080")
    calls = _routes(monkeypatch, client, direct=[403, 200], backup=[200, "down"])
    client._get("https://x/a")  # direct blocked -> backup
    client._get("https://x/b")  # backup gone -> try direct again
    assert calls == ["direct", "backup", "backup", "direct"] and client.last_route == "direct"


def test_cookies_survive_a_restart_and_expired_ones_are_dropped(tmp_path):
    import time as t
    from curl_cffi import requests as cr
    from bms_monitor.bms import load_cookies, save_cookies
    a = cr.Session()
    a.cookies.set("__cf_bm", "abc", domain=".bookmyshow.com", path="/")
    a.cookies.set("old", "x", domain=".bookmyshow.com", path="/")
    save_cookies(a, tmp_path / "c.json")
    import json
    data = json.loads((tmp_path / "c.json").read_text())
    for c in data:
        if c["name"] == "old":
            c["expires"] = int(t.time()) - 10
    (tmp_path / "c.json").write_text(json.dumps(data))
    b = cr.Session()
    load_cookies(b, tmp_path / "c.json")
    names = {c.name: c.value for c in b.cookies.jar}
    assert names == {"__cf_bm": "abc"}


def test_http_errors_keep_the_session(monkeypatch, tmp_path):
    from bms_monitor.bms import BmsClient

    class Resp:
        status_code = 403
        headers = {"cf-mitigated": "challenge"}

    client = BmsClient(cookie_dir=tmp_path)
    session = client._session(False)
    monkeypatch.setattr(session, "get", lambda url, **kw: Resp())
    with pytest.raises(BmsError, match="cloudflare challenge"):
        client._get("https://in.bookmyshow.com/x")
    assert client._sessions[False] is session  # cookies kept, not thrown away
    assert (tmp_path / "cookies-direct.json").exists()


def test_status_file_for_outside_health_checks(tmp_path):
    import json
    from bms_monitor.config import Settings
    wf = tmp_path / "watches.toml"
    wf.write_text('[[watch]]\nname = "R"\nurl = "https://in.bookmyshow.com/hyderabad/movies/ramayana/ET00451914"\n'
                  'languages = ["Telugu"]\n')
    settings = Settings(wf, tmp_path / "s.db", 60, 0, None, None, "t", "c", None)
    sent = []
    m = Monitor(settings, FakeClient("page-open.html", "showtimes-notopen.json"), Store(settings.state_db),
                lambda msg: sent.append(msg) or True)
    m.pause = lambda: None
    m.tick()
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["failures"] == 0 and st["last_good"] and st["route"] == "direct" and not st["direct_blocked"]
    assert st["watches"] == [{"name": "R", "open": False}]

    class Broken:
        def movie_page(self, ref):
            raise BmsError("HTTP 403 (direct, cloudflare challenge)")

    m.client = Broken()
    for _ in range(5):
        m.tick()
    st2 = json.loads((tmp_path / "status.json").read_text())
    assert st2["failures"] == 5 and "challenge" in st2["last_error"] and st2["last_good"] == st["last_good"]
    assert sent == []  # fail_alert_after = 0: the outside check owns the alert


def _activated_payload():
    """What BMS returned for the base code at the moment a movie went live: no dates, no shows."""
    import copy
    p = copy.deepcopy(load("showtimes-open.json"))
    for w in p["data"]["topStickyWidgets"]:
        if w.get("type") == "horizontal-block-list":
            w["data"] = []
    for w in p["data"]["showtimeWidgets"]:
        if w.get("type") == "groupList":
            w["data"] = []
    return p


def test_empty_live_response_is_activated_not_open():
    st = parse_showtimes(_activated_payload(), "ET00439706")
    assert st.activated and not st.open and st.label == "Telugu • 2D"
    st = parse_showtimes(load("showtimes-notopen.json"), "ET00439706")
    assert not st.activated and not st.open


def test_heads_up_then_open_and_fast_scans_after_opening(tmp_path):
    from bms_monitor.config import Settings
    wf = tmp_path / "watches.toml"
    wf.write_text('[[watch]]\nname = "T"\nurl = "https://in.bookmyshow.com/hyderabad/movies/talcum-powder/ET00521922"\n')
    settings = Settings(wf, tmp_path / "s.db", 60, 0, None, None, "t", "c", None)

    class Client(FakeClient):
        payload = None

        def showtimes(self, region, code, date_code="", referer=""):
            self.calls.append(("showtimes", code, date_code))
            return parse_showtimes(self.payload, code)

    client = Client("page-notopen.html", "")
    client.payload = _activated_payload()
    sent = []
    m = Monitor(settings, client, Store(settings.state_db), lambda msg: sent.append(msg) or True)
    m.pause = lambda: None
    m.tick()
    m.tick()
    assert len(sent) == 1 and "switched it on" in sent[0]  # once, and no "open" yet
    client.payload = load("showtimes-open.json")
    m.tick()
    assert "booking is open" in sent[1] and "new show" in sent[2]
    calls = len(client.calls)
    m.tick()  # within the first hour after opening: still scanned every check
    assert len(client.calls) > calls


def test_page_not_ready_is_retried_once(monkeypatch):
    from bms_monitor.bms import BmsClient

    class Resp:
        status_code = 200
        headers = {}

    pages = [
        '<script>window.__INITIAL_STATE__ = {"synopsisMoviesApi": {"queries": {"fetchSynopsisInitDynamic-x-ET00521922-rgn-HYD-": {"status": "uninitialized"}}}}</script>',
        load("page-notopen.html"),
    ]
    client = BmsClient()
    monkeypatch.setattr(client, "_get", lambda url: type("R", (), {"text": pages.pop(0)})())
    import bms_monitor.bms as b
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    page = client.movie_page(MovieRef("hyderabad", "talcum-powder", "ET00521922"))
    assert page.title == "Talcum Powder" and pages == []


def _call_settings(tmp_path, gap=300):
    from bms_monitor.config import Settings
    wf = tmp_path / "watches.toml"
    wf.write_text('[[watch]]\nname = "R"\nurl = "https://in.bookmyshow.com/hyderabad/movies/ramayana/ET00451914"\n'
                  'languages = ["Telugu"]\nformats = ["2D"]\n')
    return Settings(wf, tmp_path / "s.db", 60, 0, None, None, "t", "c", None, call_min_gap=gap)


def test_opening_call_retries_up_to_3_times(tmp_path):
    calls, ok = [], [False]
    m = Monitor(_call_settings(tmp_path), FakeClient("page-open.html", "showtimes-open.json"),
                Store(tmp_path / "s.db"), lambda msg: True, call=lambda t: calls.append(t) or ok[0])
    m.pause = lambda: None
    for _ in range(5):
        m.tick()
    assert len(calls) == 3  # opening + shows in one tick share a call; 3 failed tries, then stop
    ok[0] = True
    m.tick()
    assert len(calls) == 3


def test_new_shows_ring_again_after_the_gap(tmp_path):
    import json
    calls = []
    client = FakeClient("page-open.html", "showtimes-open.json")
    store = Store(tmp_path / "s.db")
    m = Monitor(_call_settings(tmp_path, gap=300), client, store, lambda msg: True,
                call=lambda t: calls.append(t) or True)
    m.pause = lambda: None
    m.tick()
    assert len(calls) == 1  # opening and its shows: one call

    def add_show(session):
        p = load("showtimes-open.json")
        card = p["data"]["showtimeWidgets"][1]["data"][0]["data"][0]
        st = json.loads(json.dumps(card["showtimes"][0]))
        st["additionalData"]["sessionId"] = session
        card["showtimes"].append(st)
        return p

    client.showtimes = lambda region, code, date_code="", referer="": parse_showtimes(add_show("9001"), code)
    m.tick()
    assert len(calls) == 1  # new show within 5 min: Telegram only
    store.put("last-call:hyderabad:ET00451914", "0")  # 5 minutes later
    client.showtimes = lambda region, code, date_code="", referer="": parse_showtimes(add_show("9002"), code)
    m.tick()
    assert len(calls) == 2 and calls[-1] == ""


def test_caller_builds_a_twilio_request(monkeypatch):
    import urllib.parse
    from bms_monitor import calls as c
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"status": "queued"}'

    def fake_urlopen(req, timeout):
        seen["url"], seen["body"], seen["auth"] = req.full_url, req.data, req.headers["Authorization"]
        return Resp()

    monkeypatch.setattr(c.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(c.json, "load", lambda r: {"status": "queued"})
    caller = c.Caller("AC123", "SK456", "secret", "+15550001111", ["+919999999999"])
    assert caller.call("Dune & co <booking> open")
    body = urllib.parse.parse_qs(seen["body"].decode())
    assert seen["url"].endswith("/Accounts/AC123/Calls.json") and seen["auth"].startswith("Basic ")
    assert body["To"] == ["+919999999999"] and "Dune &amp; co &lt;booking&gt; open" in body["Twiml"][0]
    assert caller.call()
    assert "<Hangup/>" in urllib.parse.parse_qs(seen["body"].decode())["Twiml"][0]
    trial = c.Caller("AC123", "SK456", "secret", "+15550001111", ["+919999999999", "+918888888888"],
                     twiml_url="https://webhooks.twilio.com/v1/Voice/Template/x")
    assert trial.call()
    body = urllib.parse.parse_qs(seen["body"].decode())
    assert body["Url"] == ["https://webhooks.twilio.com/v1/Voice/Template/x"] and "Twiml" not in body
    assert "Timeout" not in body and body["To"] == ["+918888888888"]


def test_no_call_for_an_opening_that_is_old_news(tmp_path):
    from bms_monitor.config import Settings
    wf = tmp_path / "watches.toml"
    wf.write_text('[[watch]]\nname = "R"\nurl = "https://in.bookmyshow.com/hyderabad/movies/ramayana/ET00451914"\n'
                  'languages = ["Telugu"]\nformats = ["2D"]\n')
    settings = Settings(wf, tmp_path / "s.db", 60, 0, None, None, "t", "c", None)
    store = Store(settings.state_db)
    store.mark_opened("hyderabad:ET00451914")
    store.mark_notified("hyderabad:ET00451914", [s.key for s in parse_showtimes(load("showtimes-open.json"), "ET00518312").shows])
    store.db.execute("update opened set at = at - 7200")  # opened 2 hours ago, before calls existed
    store.db.commit()
    calls = []
    m = Monitor(settings, FakeClient("page-open.html", "showtimes-open.json"), store,
                lambda msg: True, call=lambda t: calls.append(t) or True)
    m.pause = lambda: None
    m.tick()
    assert calls == []
