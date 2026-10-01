"""Request-scoped database access.

sqlite3 connections belong to one thread, so each request gets its own rather
than sharing a module-level handle with the scan worker.
"""

import sqlite3
from pathlib import Path
from typing import Iterator, Optional

from .. import config, db

# Set by create_app(); tests point it at a temporary file.
_db_path: Optional[Path] = None


def set_db_path(path: Optional[Path]) -> None:
    global _db_path
    _db_path = Path(path) if path else None


def current_db_path() -> Path:
    return _db_path or config.DB_PATH


def connect(apply_migrations: bool = False) -> sqlite3.Connection:
    """A connection that waits for the scan worker instead of failing.

    Migrations are off by default: ``migrate`` calls ``seed``, which writes, and
    doing that per request would make every page view contend for the write
    lock a scan is holding. ``migrate_once`` runs it at startup instead.

    A scan commits between articles, so the lock window is short -- but it
    exists, and without the busy timeout a dismissal landing inside it raises
    'database is locked' straight into the user's face.
    """
    return db.connect(
        current_db_path(),
        apply_migrations=apply_migrations,
        busy_timeout_ms=config.WEB_BUSY_TIMEOUT_MS,
    )


def migrate_once() -> None:
    """Apply migrations and seed, once, at startup."""
    connect(apply_migrations=True).close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency. Commits on success, rolls back on failure."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
