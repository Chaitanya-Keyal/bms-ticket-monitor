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
     dates and screens trigger new messages; nothing is sent twice. Full scans
     run every minute for the first hour after opening, then every 2 minutes
     (`BMS_OPEN_FAST_FOR`, `BMS_OPEN_INTERVAL`) since they cost the most requests.
   - optionally a **phone call** (Twilio) to one or more numbers, chosen per movie
     (`call`), with **priority cinemas** whose new shows ring a chosen list at once
     (`priority_cinemas`, `priority_call`); cinemas match by BMS ID or name. It just rings,
     at a movie's first heads-up or opening and after every new-shows alert, at
     most once per movie every 5 minutes (`CALL_MIN_GAP`);
   - optional **seat-release alerts** at chosen cinemas (`reopen_cinemas`): when a
     seat category comes back from sold out, or reaches "available" from almost
     full / filling fast, e.g. held seats released or cancellations; at most every
     10 minutes per category. Categories only, not individual seats;
   - a one-time **heads-up** when BookMyShow switches a movie on in the city
     before listing any shows (it did this about 2 minutes ahead of the shows for
     one big release).
4. If checks keep failing (blocked, site changed, network down), checks back off
   (2, 4, then 5 minutes apart) instead of retrying at full speed, and you get a
   **"checks are failing"** alert after 3 in a row (about 7 minutes), and a "working again"
   message when they recover. A failed check never counts as "not open".
5. Once a day (`BMS_HEARTBEAT_AT`) it sends a short "still watching" message, so
   a dead server or container can't pass for "not open yet".

State (what was already sent) lives in a small SQLite file. Next to it, `status.json`
is rewritten after every check for outside health monitoring: time of the last good
check, consecutive failures and the last error, the route in use, whether the direct
route is blocked, and each watch's open/not-open state. With an outside monitor reading
it, set `BMS_FAIL_ALERT_AFTER=0` and leave `BMS_HEARTBEAT_AT` empty so nothing is
reported twice.

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
across filters all must match. `skip_cinemas` drops cinemas that `cinemas` would
otherwise catch (`cinemas = ["AMB"]`, `skip_cinemas = ["AMBH"]`: every AMB but Gachibowli). Get the URL by opening the movie on BookMyShow
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
