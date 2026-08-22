"""SQLite connection management and schema migration.

Plain `sqlite3` rather than an ORM: the schema is three small tables, and the queries
are simple enough that an ORM would add a dependency and a layer of indirection
without buying maintainability.

The database is opened in WAL mode so a reader (a `/status` command) never blocks the
writer (the queue worker), and with `foreign_keys` on so a job's outputs cannot outlive
the job.

Calls here are synchronous. Async callers should wrap them in `asyncio.to_thread`;
at this workload - a handful of users on one machine - that is entirely adequate and
much easier to reason about than an async driver.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.utils.logging import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id    INTEGER NOT NULL,
    telegram_chat_id    INTEGER NOT NULL,
    telegram_message_id INTEGER,
    original_request    TEXT    NOT NULL,
    workflow_id         TEXT    NOT NULL,
    parameters          TEXT    NOT NULL DEFAULT '{}',
    status              TEXT    NOT NULL,
    comfy_prompt_id     TEXT,
    error_message       TEXT,
    user_message        TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    started_at          TEXT,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_owner   ON jobs (telegram_user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_prompt  ON jobs (comfy_prompt_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs (created_at);

CREATE TABLE IF NOT EXISTS job_outputs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    path       TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'image',
    size_bytes INTEGER,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outputs_job ON job_outputs (job_id);

-- Schema 2: people, and how they were let in.
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    role             TEXT    NOT NULL DEFAULT 'user',
    enabled          INTEGER NOT NULL DEFAULT 1,
    daily_quota      INTEGER NOT NULL DEFAULT 10,
    display_name     TEXT,
    note             TEXT,
    approved_by      INTEGER,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    code        TEXT    PRIMARY KEY,
    role        TEXT    NOT NULL DEFAULT 'user',
    daily_quota INTEGER NOT NULL DEFAULT 10,
    note        TEXT,
    created_by  INTEGER,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL,
    used_at     TEXT,
    used_by     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_invites_unused ON invites (used_at, expires_at);
"""


def to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    # Rows written before a timezone was attached should still compare correctly.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Database:
    """Owns one SQLite connection, guarded by a lock.

    A single connection keeps transaction semantics obvious. The lock makes it safe to
    share between the bot's event loop threads and the worker.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> Database:
        if self._connection is not None:
            return self

        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # explicit transactions via the `transaction` helper
            timeout=30.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        self._connection = connection

        self._migrate()
        log.info("database.connected", path=str(self._path), schema_version=SCHEMA_VERSION)
        return self

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> Database:
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------- access ----------------

    @property
    def raw(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("database is not connected; call connect() first")
        return self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a unit of work atomically. Rolls back on any exception."""
        with self._lock:
            connection = self.raw
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.raw.execute(sql, params)

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    # ---------------- migration ----------------

    def _migrate(self) -> None:
        connection = self.raw
        connection.executescript(SCHEMA)

        current = connection.execute("PRAGMA user_version").fetchone()[0]
        if current == 0:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database at {self._path} was written by a newer version "
                f"(schema {current} > {SCHEMA_VERSION}). Refusing to open it."
            )
        elif current < SCHEMA_VERSION:
            # Future migrations land here, stepping one version at a time.
            log.info("database.migrating", from_version=current, to_version=SCHEMA_VERSION)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
