# bms-ticket-monitor

Telegram alerts the moment BookMyShow opens bookings that match your filters:
movie, city, language, format (IMAX, 4DX, 3D...), screen type (LASER, ATMOS...),
cinema, date and show time. It never books anything; it only tells you.

## How it works

Every minute (randomised a little), for each watch:

1. Load the movie's page for your city. Until BookMyShow shows the **Book tickets**
   button there, it also asks the showtimes API about the movie's own code (one
   small request), so a page layout change can't hide an opening.
2. Once it's open, the button lists every language/format version with its own
   event code. The versions your filters allow are fetched from the same
   showtimes API the website uses, for your dates (or the first 3 bookable dates).
3. Each show is matched against your filters. You get:
   - one **"booking is open"** message per movie and city, once a version you
     watch has shows there, listing those versions;
   - a message for every show matching your filters that you haven't been told
     about yet, grouped by day and cinema, with a booking link. Later cinemas,
     dates and screens trigger new messages; nothing is sent twice. After the
     "open" message, full scans run every 2 minutes (`BMS_OPEN_INTERVAL`) since
     they cost the most requests.
4. If checks keep failing (blocked, site changed, network down), checks back off
   (2, 4, then 5 minutes apart) instead of retrying at full speed, and you get a
   **"checks are failing"** alert after 3 in a row (about 7 minutes), and a "working again"
   message when they recover. A failed check never counts as "not open".
5. Once a day (`BMS_HEARTBEAT_AT`) it sends a short "still watching" message, so
   a dead server or container can't pass for "not open yet".

State (what was already sent) lives in a small SQLite file.

## Watches

`config/watches.toml` (the `BMS_WATCHES` path), re-read on every check, so edits apply
without a restart.
See [watches.example.toml](watches.example.toml) for every option.

```toml
[[watch]]
name = "Some movie, IMAX on release weekend"
url = "https://in.bookmyshow.com/hyderabad/movies/some-movie/ET00000000"
languages = ["English"]
formats = ["IMAX"]
cinemas = ["Prasads", "AMB"]
dates = ["2026-12-18", "2026-12-19"]
after = "18:00"
```

All filters are optional, case-insensitive and match on parts of names
(`"AMB"` matches "AMB Cinemas: Gachibowli"). Within a filter any value matches;
across filters all must match. Get the URL by opening the movie on BookMyShow
in your city and copying the address.

## Running

```sh
cp .env.example .env              # bot token + chat id
mkdir config && cp watches.example.toml config/watches.toml
docker compose up -d --build
```

Without Docker: `pip install .`, then `python -m bms_monitor run`.

- `python -m bms_monitor check` does one pass and prints what it would send (sends nothing).
- `python -m bms_monitor telegram-test` sends a test message.

Settings are environment variables; see [.env.example](.env.example).

## Notes

- BookMyShow sits behind Cloudflare's bot protection. Requests use `curl_cffi`
  with a Chrome fingerprint, one cookie session and Indian English headers.
  Cookies are saved, so restarts keep an accepted session. Datacenter IPs may
  still get blocked; `BMS_PROXY` adds a backup route (e.g. a home connection)
  that's used only while the direct one is blocked, so direct stays the main,
  always-warm route.
- This is a personal tool. It makes one request per watched movie per minute
  until booking opens, and a few more once it's open.
- Tests run against saved responses in `tests/fixtures`:
  `pip install -e . pytest && pytest`. `scripts/capture.py` saves fresh ones.
