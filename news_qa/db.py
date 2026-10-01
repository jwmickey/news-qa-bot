"""SQLite storage: schema, migrations, and small query helpers.

Plain sqlite3 with a numbered-migration runner. The schema is small enough that
an ORM would cost more than it saves, and keeping the DDL literal makes the
data model easy to read.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import config
from .sources import SOURCES

# Rules muted out of the box. These are formatting opinions rather than errors:
# EN_QUOTES flags straight vs. curly quotes, WHITESPACE_RULE flags spacing that
# is usually an artifact of HTML extraction.
DEFAULT_SUPPRESSED_RULES = [
    ("EN_QUOTES", "Straight vs. curly quotes -- a house style choice, not an error."),
    ("WHITESPACE_RULE", "Spacing noise, usually an extraction artifact."),
]

# Each migration is (version, [sql statements]). Append only; never edit a
# migration that has shipped.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """
            CREATE TABLE sources (
                id        INTEGER PRIMARY KEY,
                key       TEXT NOT NULL UNIQUE,
                name      TEXT NOT NULL,
                feed_url  TEXT NOT NULL,
                homepage  TEXT NOT NULL DEFAULT '',
                active    INTEGER NOT NULL DEFAULT 1
            )
            """,
            """
            CREATE TABLE articles (
                id               INTEGER PRIMARY KEY,
                source_id        INTEGER NOT NULL REFERENCES sources(id),
                url              TEXT NOT NULL UNIQUE,
                canonical_url    TEXT,
                title            TEXT,
                byline           TEXT,
                published_at     TEXT,
                modified_at      TEXT,
                first_seen_at    TEXT NOT NULL,
                last_scanned_at  TEXT,
                extraction_status TEXT NOT NULL DEFAULT 'pending',
                extraction_note  TEXT,
                http_etag        TEXT,
                http_last_modified TEXT
            )
            """,
            "CREATE INDEX idx_articles_source ON articles(source_id)",
            """
            CREATE TABLE scans (
                id               INTEGER PRIMARY KEY,
                source_id        INTEGER REFERENCES sources(id),
                kind             TEXT NOT NULL DEFAULT 'scan',
                started_at       TEXT NOT NULL,
                finished_at      TEXT,
                status           TEXT NOT NULL DEFAULT 'running',
                articles_seen    INTEGER NOT NULL DEFAULT 0,
                articles_checked INTEGER NOT NULL DEFAULT 0,
                articles_failed  INTEGER NOT NULL DEFAULT 0,
                articles_unchanged INTEGER NOT NULL DEFAULT 0,
                issues_new       INTEGER NOT NULL DEFAULT 0,
                issues_resolved  INTEGER NOT NULL DEFAULT 0,
                note             TEXT
            )
            """,
            """
            CREATE TABLE article_versions (
                id              INTEGER PRIMARY KEY,
                article_id      INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                scan_id         INTEGER REFERENCES scans(id),
                content_hash    TEXT NOT NULL,
                body_json       TEXT NOT NULL,
                paragraph_count INTEGER NOT NULL DEFAULT 0,
                title           TEXT,
                byline          TEXT,
                fetched_at      TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_versions_article ON article_versions(article_id)",
            "CREATE INDEX idx_versions_hash ON article_versions(article_id, content_hash)",
            """
            CREATE TABLE issues (
                id                  INTEGER PRIMARY KEY,
                article_id          INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                fingerprint         TEXT NOT NULL,
                kind                TEXT NOT NULL,
                rule_id             TEXT,
                category            TEXT,
                severity            TEXT NOT NULL DEFAULT 'error',
                message             TEXT NOT NULL,
                matched_text        TEXT,
                context_before      TEXT,
                context_after       TEXT,
                suggestions_json    TEXT,
                para_index          INTEGER,
                char_offset         INTEGER,
                match_length        INTEGER,
                in_quote            INTEGER NOT NULL DEFAULT 0,
                first_version_id    INTEGER REFERENCES article_versions(id),
                last_seen_version_id INTEGER REFERENCES article_versions(id),
                first_scan_id       INTEGER REFERENCES scans(id),
                last_scan_id        INTEGER REFERENCES scans(id),
                status              TEXT NOT NULL DEFAULT 'open',
                suppressed_reason   TEXT,
                resolved_at         TEXT,
                created_at          TEXT NOT NULL,
                review_json         TEXT,
                UNIQUE(article_id, fingerprint)
            )
            """,
            "CREATE INDEX idx_issues_status ON issues(status)",
            "CREATE INDEX idx_issues_article ON issues(article_id)",
            "CREATE INDEX idx_issues_scan ON issues(last_scan_id)",
            """
            CREATE TABLE dismissals (
                id         INTEGER PRIMARY KEY,
                issue_id   INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
                reason     TEXT NOT NULL,
                note       TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE dictionary (
                id                  INTEGER PRIMARY KEY,
                term                TEXT NOT NULL COLLATE NOCASE UNIQUE,
                note                TEXT,
                added_at            TEXT NOT NULL,
                added_from_issue_id INTEGER REFERENCES issues(id)
            )
            """,
            """
            CREATE TABLE suppressions (
                id         INTEGER PRIMARY KEY,
                rule_id    TEXT NOT NULL,
                scope      TEXT NOT NULL DEFAULT 'global',
                note       TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(rule_id, scope)
            )
            """,
        ],
    ),
    (
        2,
        [
            # A queued job has no scans row yet -- scan() inserts that only when
            # the worker actually starts it -- so the web queue needs its own
            # table to hold intent, parameters, and live progress.
            """
            CREATE TABLE scan_jobs (
                id           INTEGER PRIMARY KEY,
                source_id    INTEGER NOT NULL REFERENCES sources(id),
                kind         TEXT NOT NULL DEFAULT 'scan',
                params_json  TEXT,
                state        TEXT NOT NULL DEFAULT 'queued',
                scan_id      INTEGER REFERENCES scans(id),
                requested_at TEXT NOT NULL,
                started_at   TEXT,
                finished_at  TEXT,
                progress     TEXT,
                error        TEXT
            )
            """,
            "CREATE INDEX idx_scan_jobs_state ON scan_jobs(state)",
            # Reopening an issue must not erase the record of the judgement that
            # dismissed it, so the row is stamped rather than deleted.
            "ALTER TABLE dismissals ADD COLUMN revoked_at TEXT",
        ],
    ),
]


def utcnow() -> str:
    """Timestamp string used for every stored datetime."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(
    path: Optional[Path] = None,
    apply_migrations: bool = True,
    busy_timeout_ms: int = 0,
) -> sqlite3.Connection:
    """Open a connection with sane defaults and, by default, the schema applied.

    ``apply_migrations=False`` matters for anything that opens a connection
    often. ``migrate`` calls ``seed``, which *writes*, so migrating on every
    connection would take the single write lock every time -- for the web UI
    that means every page view queues behind a running scan. The server
    migrates once at startup and opens its per-request connections with this
    off.

    ``busy_timeout_ms`` is applied before any statement runs, so a caller that
    is willing to wait for the lock actually does.
    """
    db_path = Path(path) if path else config.DB_PATH
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if busy_timeout_ms:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if apply_migrations:
        migrate(conn)
    return conn


@contextmanager
def session(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> None:
    """Apply any migrations the database has not seen yet."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] or 0

    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        for statement in statements:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        current = version

    conn.commit()
    seed(conn)


def seed(conn: sqlite3.Connection) -> None:
    """Insert baseline rows that the app expects to exist. Idempotent."""
    now = utcnow()

    for source in SOURCES:
        conn.execute(
            """
            INSERT INTO sources (key, name, feed_url, homepage, active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                name = excluded.name,
                feed_url = excluded.feed_url,
                homepage = excluded.homepage,
                active = excluded.active
            """,
            (source.key, source.name, source.feed_url, source.homepage, int(source.active)),
        )

    for rule_id, note in DEFAULT_SUPPRESSED_RULES:
        conn.execute(
            """
            INSERT INTO suppressions (rule_id, scope, note, created_at)
            VALUES (?, 'global', ?, ?)
            ON CONFLICT(rule_id, scope) DO NOTHING
            """,
            (rule_id, note, now),
        )

    # The seed dictionary only primes an empty table; it never overwrites terms
    # the user has curated, and removing a term from the file will not re-add it.
    already_seeded = conn.execute("SELECT COUNT(*) AS n FROM dictionary").fetchone()["n"]
    if not already_seeded and config.SEED_DICTIONARY_PATH.exists():
        terms = [
            line.strip()
            for line in config.SEED_DICTIONARY_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        conn.executemany(
            """
            INSERT INTO dictionary (term, note, added_at) VALUES (?, 'seed', ?)
            ON CONFLICT(term) DO NOTHING
            """,
            [(term, now) for term in terms],
        )

    conn.commit()


# --- Small helpers used across modules ------------------------------------


def source_id(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT id FROM sources WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(f"Source {key!r} is not in the database")
    return row["id"]


def load_dictionary(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT term FROM dictionary").fetchall()
    return {row["term"].casefold() for row in rows}


def load_suppressed_rules(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT rule_id FROM suppressions WHERE scope = 'global'").fetchall()
    return {row["rule_id"] for row in rows}


def add_dictionary_term(
    conn: sqlite3.Connection,
    term: str,
    note: Optional[str] = None,
    issue_id: Optional[int] = None,
) -> bool:
    """Add a term to the allowlist. Returns False if it was already present."""
    cursor = conn.execute(
        """
        INSERT INTO dictionary (term, note, added_at, added_from_issue_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(term) DO NOTHING
        """,
        (term.strip(), note, utcnow(), issue_id),
    )
    return cursor.rowcount > 0
