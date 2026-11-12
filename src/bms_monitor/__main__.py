"""Command line.

    python -m bms_monitor run              check every minute and send alerts
    python -m bms_monitor check            one pass, print what would be sent (sends nothing)
    python -m bms_monitor telegram-test    send a test message
"""

from __future__ import annotations

import argparse
import html
import logging
import sys

from .bms import BmsClient, BmsError
from .config import Settings, load_watches
from .monitor import Monitor, check_watch, opened_message, shows_message
from .store import Store
from .telegram import Telegram


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bms_monitor")
    parser.add_argument("command", choices=["run", "check", "telegram-test"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    settings = Settings.from_env()

    if args.command == "check":
        client = BmsClient(proxy=settings.proxy)
        for watch in load_watches(settings.watches_file):
            try:
                r = check_watch(client, watch)
            except BmsError as e:
                print(f"## {watch.name}: CHECK FAILED: {e}\n")
                continue
            print(f"## {r.page.title} [{watch.name}]: {'OPEN' if r.open else 'not open'} "
                  f"(region {r.page.region}, {len(r.page.variants)} versions listed, "
                  f"{len(r.open_versions)} watched versions with shows, {len(r.matches)} matching shows)")
            if r.open:
                print(opened_message(r))
            if r.matches:
                print("\n" + shows_message(r, r.matches))
            print()
        return 0

    telegram = Telegram(settings.telegram_token, settings.telegram_chat_id, settings.telegram_thread_id)
    if args.command == "telegram-test":
        return 0 if telegram.send("✅ BookMyShow monitor can reach you here.") else 1

    watches = load_watches(settings.watches_file)
    names = ", ".join(html.escape(f"{w.name} ({w.movie.city.title()})") for w in watches if w.enabled)
    client = BmsClient(proxy=settings.proxy, cookie_dir=settings.state_db.parent)
    monitor = Monitor(settings, client, Store(settings.state_db), telegram.send)
    telegram.send(f"👀 BookMyShow monitor started. Watching: {names or 'nothing yet'}")
    monitor.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
