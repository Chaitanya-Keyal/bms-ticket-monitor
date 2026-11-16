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


def _numbers(value: str) -> list[tuple[str, str]]:
    """'Asha=+919800000000, +919811111111' -> [('Asha', '+919800000000'), ('...1111', '+919811111111')]"""
    out = []
    for entry in value.split(","):
        label, _, number = entry.strip().rpartition("=")
        number = number.replace(" ", "")
        if number:
            out.append((label.strip() or f"...{number[-4:]}", number))
    return out


def _secret(env, name: str) -> str:
    value = env.get(name, "")
    if not value and env.get(f"{name}_FILE"):
        value = Path(env[f"{name}_FILE"]).read_text().strip()
    return value


@dataclass
class Settings:
    watches_file: Path
    state_db: Path
    interval: float
    fail_alert_after: int
    proxy: str | None
    heartbeat_at: int | None  # HHMM local time (TZ), daily "still watching" message
    telegram_token: str
    telegram_chat_id: str
    telegram_thread_id: str | None
    open_interval: float = 120  # seconds between full scans once a watch's booking is open
    open_fast_for: float = 3600  # ...except for this long after opening, when it's every check
    twilio_account_sid: str = ""
    twilio_key_sid: str = ""
    twilio_key_secret: str = ""
    call_from: str = ""
    call_to: list[tuple[str, str]] = field(default_factory=list)  # (label, number); empty: no calls
    call_min_gap: float = 300  # seconds between calls about the same movie
    call_twiml_url: str = ""  # what calls play; trial accounts must use a Twilio template URL

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
            fail_alert_after=int(env.get("BMS_FAIL_ALERT_AFTER", "3")),
            proxy=env.get("BMS_PROXY") or None,
            heartbeat_at=_time(env.get("BMS_HEARTBEAT_AT") or None),
            telegram_token=token,
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
            telegram_thread_id=env.get("TELEGRAM_THREAD_ID") or None,
            open_interval=float(env.get("BMS_OPEN_INTERVAL", "120")),
            open_fast_for=float(env.get("BMS_OPEN_FAST_FOR", "3600")),
            twilio_account_sid=env.get("TWILIO_ACCOUNT_SID", ""),
            twilio_key_sid=env.get("TWILIO_API_KEY_SID", ""),
            twilio_key_secret=_secret(env, "TWILIO_API_KEY_SECRET"),
            call_from=env.get("CALL_FROM", ""),
            call_to=_numbers(env.get("CALL_TO", "")),
            call_min_gap=float(env.get("CALL_MIN_GAP", "300")),
            call_twiml_url=env.get("CALL_TWIML_URL", ""),
        )
