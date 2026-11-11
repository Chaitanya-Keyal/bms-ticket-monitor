"""Save raw BookMyShow responses as test fixtures.

This is the only place in the repo that talks to BookMyShow outside the
running monitor. It makes a handful of requests, a couple of seconds apart.

    python scripts/capture.py pages OUT_DIR CITY_SLUG
    python scripts/capture.py showtimes OUT_DIR REGION_CODE EVENT_CODE [DATE_CODE] [NAME]
    python scripts/capture.py page OUT_DIR URL NAME
"""

import sys
import time
from pathlib import Path

from curl_cffi import requests

BASE = "https://in.bookmyshow.com"
SHOWTIMES = BASE + "/api/movies-data/v4/showtimes-by-event/primary-dynamic"


def session() -> requests.Session:
    return requests.Session(impersonate="chrome")


def save(out: Path, name: str, resp) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ext = "json" if "json" in (resp.headers.get("content-type") or "") else "html"
    path = out / f"{name}.{ext}"
    path.write_bytes(resp.content)
    print(f"{resp.status_code} {len(resp.content):>8} bytes  {path}")


def pages(out: Path, city: str) -> None:
    s = session()
    for name, url in [
        (f"explore-{city}", f"{BASE}/explore/movies-{city}"),
        (f"upcoming-{city}", f"{BASE}/explore/upcoming-movies-{city}"),
    ]:
        save(out, name, s.get(url, timeout=20))
        time.sleep(2)


def showtimes(out: Path, region: str, event: str, date: str = "", name: str = "") -> None:
    s = session()
    params = {"eventCode": event, "dateCode": date, "isDesktop": "true", "regionCode": region,
              "xLocationShared": "false", "memberId": "", "lsId": "", "subCode": ""}
    headers = {"x-app-code": "WEB", "x-region-code": region}
    resp = s.get(SHOWTIMES, params=params, headers=headers, timeout=20)
    save(out, name or f"showtimes-{event}-{date or 'default'}", resp)


def page(out: Path, url: str, name: str) -> None:
    save(out, name, session().get(url, timeout=20))


if __name__ == "__main__":
    cmd, out, *rest = sys.argv[1:]
    {"pages": pages, "showtimes": showtimes, "page": page}[cmd](Path(out), *rest)
