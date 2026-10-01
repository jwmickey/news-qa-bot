"""Background scan queue.

One worker thread runs one scan at a time. That is not a placeholder for a real
job queue -- it is the correct shape here. A scan holds the single SQLite write
lock, starts a Java LanguageTool server, and deliberately waits ~1.5s between
requests to a host; running two at once would multiply the cost and gain
nothing. Sources are scanned in turn rather than in parallel.

Two things make a long scan tolerable to a UI:

* The worker commits after every article, via ``scan()``'s ``on_progress``
  hook. Without that, ``scan()``'s single transaction would hold the write lock
  for the whole run and every dismissal in the browser would block behind it.
* One ``GrammarChecker`` is created on the worker thread and reused for the
  process lifetime, so the JVM starts once rather than once per scan.
"""

import json
import queue
import sqlite3
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from .. import config, db
from ..detect import GrammarChecker
from ..scan import ArticleResult, scan


class JobRejected(Exception):
    """Raised when an equivalent job is already queued or running."""


@dataclass(frozen=True)
class JobParams:
    limit: Optional[int] = None
    since_days: Optional[int] = None
    open_only: bool = True
    use_ner: bool = True

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, raw: Optional[str]) -> "JobParams":
        if not raw:
            return cls()
        data = json.loads(raw)
        return cls(**{key: data[key] for key in data if key in cls.__annotations__})


def describe(result: ArticleResult) -> str:
    """One line of progress, worded like the CLI's so both agree."""
    label = result.title or result.url
    if result.outcome == "checked":
        visible = len(result.visible_issues)
        hidden = len(result.issues) - visible
        detail = f"{visible} issue(s)" + (f", {hidden} hidden" if hidden else "")
        if result.resolved_issues:
            detail += f", {result.resolved_issues} resolved"
    else:
        detail = result.note or result.outcome
    return f"{label} — {detail}"


def reap_orphans(conn: sqlite3.Connection) -> int:
    """Fail anything left mid-flight by a crash or restart.

    Without this a killed server leaves a scan reading 'running' forever, and
    the duplicate-job guard would refuse every future scan of that source.
    """
    note = "interrupted by restart"
    jobs = conn.execute(
        "UPDATE scan_jobs SET state = 'error', error = ?, finished_at = ? "
        "WHERE state IN ('queued', 'running')",
        (note, db.utcnow()),
    ).rowcount
    conn.execute(
        "UPDATE scans SET status = 'error', note = COALESCE(note, ?), finished_at = ? "
        "WHERE status = 'running'",
        (note, db.utcnow()),
    )
    conn.commit()
    return jobs


class ScanQueue:
    """Owns the worker thread and the pending-job queue."""

    def __init__(
        self,
        db_path=None,
        checker_factory: Optional[Callable[[], GrammarChecker]] = None,
        fetcher_factory: Optional[Callable[[], object]] = None,
    ) -> None:
        self._db_path = db_path
        self._checker_factory = checker_factory or GrammarChecker
        self._fetcher_factory = fetcher_factory
        self._queue: "queue.Queue[Optional[int]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._checker: Optional[GrammarChecker] = None
        self._current_job: Optional[int] = None
        self._lock = threading.Lock()

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="scan-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_job_id(self) -> Optional[int]:
        with self._lock:
            return self._current_job

    def _connect(self) -> sqlite3.Connection:
        """The worker's own connection -- sqlite3 handles are not shareable.

        Migrations stay on here: a ScanQueue is also usable without the web app
        (and in tests), and one migrate per job is not a hot path.
        """
        return db.connect(self._db_path, busy_timeout_ms=config.WEB_BUSY_TIMEOUT_MS)

    # --- enqueueing -------------------------------------------------------

    def enqueue(
        self,
        conn: sqlite3.Connection,
        source_key: str,
        kind: str = "scan",
        params: Optional[JobParams] = None,
    ) -> int:
        """Queue a scan. Commits immediately so the worker can see the row."""
        if kind not in ("scan", "rescan"):
            raise ValueError(f"Unknown scan kind {kind!r}")
        params = params or JobParams()
        source_id = db.source_id(conn, source_key)

        clash = conn.execute(
            "SELECT id FROM scan_jobs WHERE source_id = ? AND kind = ? "
            "AND state IN ('queued', 'running')",
            (source_id, kind),
        ).fetchone()
        if clash:
            raise JobRejected(f"A {kind} of {source_key} is already queued (job #{clash['id']}).")

        cursor = conn.execute(
            "INSERT INTO scan_jobs (source_id, kind, params_json, state, requested_at) "
            "VALUES (?, ?, ?, 'queued', ?)",
            (source_id, kind, params.to_json(), db.utcnow()),
        )
        conn.commit()
        job_id = cursor.lastrowid
        self._queue.put(job_id)
        return job_id

    # --- the worker -------------------------------------------------------

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                break
            try:
                self._run(job_id)
            except BaseException as error:  # noqa: BLE001 - the worker must survive
                self._mark_failed(job_id, error)
        self._close_checker()

    def _run(self, job_id: int) -> None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["state"] != "queued":
                return  # reaped by a restart, or already handled

            source_key = conn.execute(
                "SELECT key FROM sources WHERE id = ?", (row["source_id"],)
            ).fetchone()["key"]
            params = JobParams.from_json(row["params_json"])
            rescan = row["kind"] == "rescan"

            with self._lock:
                self._current_job = job_id
            conn.execute(
                "UPDATE scan_jobs SET state = 'running', started_at = ?, progress = ? WHERE id = ?",
                (db.utcnow(), "starting…", job_id),
            )
            conn.commit()

            def on_progress(result: ArticleResult) -> None:
                self._record_progress(conn, job_id, row["source_id"], result)

            try:
                summary = scan(
                    conn,
                    source_key=source_key,
                    limit=params.limit,
                    rescan=rescan,
                    open_only=params.open_only,
                    since_days=params.since_days,
                    use_ner=params.use_ner,
                    checker=self._get_checker(),
                    fetcher=self._fetcher_factory() if self._fetcher_factory else None,
                    on_progress=on_progress,
                )
            except BaseException as error:  # noqa: BLE001 - CLI-equivalent boundary
                # scan() has already marked its own row 'error'; commit that
                # verdict along with everything the scan managed to finish.
                conn.commit()
                conn.execute(
                    "UPDATE scan_jobs SET state = 'error', error = ?, finished_at = ? WHERE id = ?",
                    (str(error) or error.__class__.__name__, db.utcnow(), job_id),
                )
                conn.commit()
                return

            conn.execute(
                """
                UPDATE scan_jobs SET
                    state = 'done', scan_id = ?, finished_at = ?, progress = ?
                WHERE id = ?
                """,
                (
                    summary.scan_id,
                    db.utcnow(),
                    f"{summary.articles_checked} checked, {summary.issues_new} new, "
                    f"{summary.issues_resolved} resolved",
                    job_id,
                ),
            )
            conn.commit()
        finally:
            with self._lock:
                self._current_job = None
            conn.close()

    def _record_progress(
        self, conn: sqlite3.Connection, job_id: int, source_id: int, result: ArticleResult
    ) -> None:
        """Commit one article's work and publish a progress line.

        The commit is the point: it releases the write lock between articles so
        the rest of the UI keeps working while a scan runs.
        """
        conn.execute(
            "UPDATE scan_jobs SET progress = ?, scan_id = COALESCE(scan_id, ("
            "  SELECT id FROM scans WHERE source_id = ? AND status = 'running'"
            "  ORDER BY id DESC LIMIT 1"
            ")) WHERE id = ?",
            (describe(result), source_id, job_id),
        )
        conn.commit()

    def _mark_failed(self, job_id: int, error: BaseException) -> None:
        try:
            conn = self._connect()
            conn.execute(
                "UPDATE scan_jobs SET state = 'error', error = ?, finished_at = ? WHERE id = ?",
                (str(error) or error.__class__.__name__, db.utcnow(), job_id),
            )
            conn.commit()
            conn.close()
        except Exception:  # noqa: BLE001 - nothing useful left to do
            pass

    # --- the shared LanguageTool server -----------------------------------

    def _get_checker(self) -> GrammarChecker:
        if self._checker is None:
            self._checker = self._checker_factory()
        return self._checker

    def _close_checker(self) -> None:
        if self._checker is not None:
            try:
                self._checker.close()
            except Exception:  # noqa: BLE001
                pass
            self._checker = None
