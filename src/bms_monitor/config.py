"""Settings (environment) and watches (TOML file)."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .bms import MovieRef, parse_movie_url


@dataclass
class Watch:
    name: str
    movie: MovieRef
    languages: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    cinemas: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)  # YYYYMMDD
    after: int | None = None  # HHMM, inclusive
    before: int | None = None  # HHMM, inclusive
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"{self.movie.city}:{self.movie.code}"


def _strings(raw: dict, name: str) -> list[str]:
    value = raw.get(name, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"'{name}' must be a list of strings")
    return [v.strip() for v in value if v.strip()]


def _date(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"20\d{6}", digits):
        raise ValueError(f"bad date {value!r}, use YYYY-MM-DD")
    return digits


def _time(value: str | None) -> int | None:
    if value is None:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"bad time {value!r}, use HH:MM (24-hour)")
    return int(m.group(1)) * 100 + int(m.group(2))


def load_watches(path: Path) -> list[Watch]:
    with open(path, "rb") as f:
        doc = tomllib.load(f)
    watches = []
    for i, raw in enumerate(doc.get("watch", []), 1):
        try:
            movie = parse_movie_url(raw["url"])
            watches.append(Watch(
                name=raw.get("name") or movie.slug,
                movie=movie,
                languages=_strings(raw, "languages"),
                formats=_strings(raw, "formats"),
                cinemas=_strings(raw, "cinemas"),
                dates=[_date(d) for d in _strings(raw, "dates")],
                after=_time(raw.get("after")),
                before=_time(raw.get("before")),
                enabled=bool(raw.get("enabled", True)),
            ))
        except (KeyError, ValueError) as e:
            raise ValueError(f"watch #{i} in {path}: {e}") from e
    return watches


@dataclass
class Settings:
    watches_file: Path
    state_db: Path
    interval: float
    fail_alert_after: int
    proxy: str | None
    telegram_token: str
    telegram_chat_id: str
    telegram_thread_id: str | None

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        token_file = env.get("TELEGRAM_BOT_TOKEN_FILE")
        if not token and token_file:
            token = Path(token_file).read_text().strip()
        return cls(
            watches_file=Path(env.get("BMS_WATCHES", "watches.toml")),
            state_db=Path(env.get("BMS_STATE_DB", "state.db")),
            interval=float(env.get("BMS_INTERVAL", "60")),
            fail_alert_after=int(env.get("BMS_FAIL_ALERT_AFTER", "5")),
            proxy=env.get("BMS_PROXY") or None,
            telegram_token=token,
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
            telegram_thread_id=env.get("TELEGRAM_THREAD_ID") or None,
        )
