"""Small SQLite state: what was already sent, and check health."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
create table if not exists notified (
    watch text not null,
    show text not null,
    at integer not null,
    primary key (watch, show)
);
create table if not exists opened (
    watch text primary key,
    at integer not null
);
create table if not exists health (
    id integer primary key check (id = 1),
    failures integer not null default 0,
    failing_since integer,
    last_error text,
    alerted integer not null default 0
);
insert or ignore into health (id) values (1);
create table if not exists meta (
    key text primary key,
    value text not null
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def notified(self, watch: str, shows: list[str]) -> set[str]:
        if not shows:
            return set()
        rows = self.db.execute(
            f"select show from notified where watch = ? and show in ({','.join('?' * len(shows))})",
            [watch, *shows],
        )
        return {r[0] for r in rows}

    def mark_notified(self, watch: str, shows: list[str]) -> None:
        now = int(time.time())
        self.db.executemany("insert or ignore into notified values (?, ?, ?)",
                            [(watch, s, now) for s in shows])
        self.db.commit()

    def was_opened(self, watch: str) -> bool:
        return self.db.execute("select 1 from opened where watch = ?", [watch]).fetchone() is not None

    def opened_at(self, watch: str) -> int | None:
        row = self.db.execute("select at from opened where watch = ?", [watch]).fetchone()
        return row[0] if row else None

    def mark_opened(self, watch: str) -> None:
        self.db.execute("insert or ignore into opened values (?, ?)", [watch, int(time.time())])
        self.db.commit()

    def health(self) -> tuple[int, int | None, str | None, bool]:
        f, since, err, alerted = self.db.execute(
            "select failures, failing_since, last_error, alerted from health where id = 1").fetchone()
        return f, since, err, bool(alerted)

    def record_failure(self, error: str) -> int:
        self.db.execute(
            "update health set failures = failures + 1, last_error = ?,"
            " failing_since = coalesce(failing_since, ?) where id = 1",
            [error, int(time.time())])
        self.db.commit()
        return self.health()[0]

    def set_alerted(self, alerted: bool) -> None:
        self.db.execute("update health set alerted = ? where id = 1", [int(alerted)])
        self.db.commit()

    def get(self, key: str) -> str | None:
        row = self.db.execute("select value from meta where key = ?", [key]).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        self.db.execute("insert into meta values (?, ?) on conflict (key) do update set value = excluded.value",
                        [key, value])
        self.db.commit()

    def record_success(self) -> None:
        self.db.execute("update health set failures = 0, failing_since = null, last_error = null where id = 1")
        self.db.commit()
