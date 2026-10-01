"""Scan orchestration: fetch, extract, detect, persist.

The scanner never commits; the caller decides. That is what makes ``--dry-run``
a single code path -- do the whole scan, then roll back.

Two correctness rules govern how issues are resolved, because a false "fixed"
signal is worse than no signal at all:

* Issues are only resolved for an article we successfully re-read. A fetch
  failure or an extraction failure resolves nothing.
* Only ``open`` issues are resolved. A dismissed issue that disappears stays
  dismissed, so the record of your judgement is never overwritten.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence

from . import db
from .detect import FilterChain, GrammarChecker, Issue, run_detectors
from .extract import ExtractedArticle, extract_article
from .fetch import Fetcher
from .sources import Source, get_source


@dataclass
class ArticleResult:
    url: str
    title: Optional[str] = None
    outcome: str = "checked"  # checked | unchanged | failed | skipped
    note: Optional[str] = None
    article_id: Optional[int] = None
    issues: List[Issue] = field(default_factory=list)
    new_issues: int = 0
    resolved_issues: int = 0

    @property
    def visible_issues(self) -> List[Issue]:
        return [issue for issue in self.issues if not issue.suppressed]


@dataclass
class ScanSummary:
    scan_id: Optional[int] = None
    source_key: str = ""
    kind: str = "scan"
    articles_seen: int = 0
    articles_checked: int = 0
    articles_failed: int = 0
    articles_unchanged: int = 0
    issues_new: int = 0
    issues_resolved: int = 0
    results: List[ArticleResult] = field(default_factory=list)

    @property
    def flagged(self) -> List[ArticleResult]:
        return [result for result in self.results if result.visible_issues]


# --- persistence helpers --------------------------------------------------


def upsert_article(
    conn: sqlite3.Connection, source_id: int, url: str, title: Optional[str]
) -> int:
    row = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
    if row:
        return row["id"]
    cursor = conn.execute(
        """
        INSERT INTO articles (source_id, url, title, first_seen_at)
        VALUES (?, ?, ?, ?)
        """,
        (source_id, url, title, db.utcnow()),
    )
    return cursor.lastrowid


def latest_version(conn: sqlite3.Connection, article_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM article_versions
        WHERE article_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (article_id,),
    ).fetchone()


def insert_version(
    conn: sqlite3.Connection,
    article_id: int,
    scan_id: Optional[int],
    article: ExtractedArticle,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO article_versions
            (article_id, scan_id, content_hash, body_json, paragraph_count,
             title, byline, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article_id,
            scan_id,
            article.content_hash,
            article.body_json(),
            len(article.paragraphs),
            article.title,
            article.byline,
            db.utcnow(),
        ),
    )
    return cursor.lastrowid


def persist_issues(
    conn: sqlite3.Connection,
    article_id: int,
    version_id: int,
    scan_id: Optional[int],
    issues: Sequence[Issue],
) -> tuple:
    """Upsert detected issues and resolve the ones that have disappeared.

    Returns (new_count, resolved_count).
    """
    now = db.utcnow()
    existing = {
        row["fingerprint"]: row
        for row in conn.execute(
            "SELECT id, fingerprint, status FROM issues WHERE article_id = ?",
            (article_id,),
        )
    }

    new_count = 0
    seen_fingerprints = set()

    for issue in issues:
        seen_fingerprints.add(issue.fingerprint)
        suggestions = ",".join(issue.suggestions) if issue.suggestions else None
        row = existing.get(issue.fingerprint)

        if row is None:
            conn.execute(
                """
                INSERT INTO issues
                    (article_id, fingerprint, kind, rule_id, category, severity,
                     message, matched_text, context_before, context_after,
                     suggestions_json, para_index, char_offset, match_length,
                     in_quote, first_version_id, last_seen_version_id,
                     first_scan_id, last_scan_id, status, suppressed_reason,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'open', ?, ?)
                """,
                (
                    article_id,
                    issue.fingerprint,
                    issue.kind,
                    issue.rule_id,
                    issue.category,
                    issue.severity,
                    issue.message,
                    issue.matched_text,
                    issue.context_before,
                    issue.context_after,
                    suggestions,
                    issue.para_index,
                    issue.char_offset,
                    issue.match_length,
                    int(issue.in_quote),
                    version_id,
                    version_id,
                    scan_id,
                    scan_id,
                    issue.suppressed_reason,
                    now,
                ),
            )
            if not issue.suppressed:
                new_count += 1
            continue

        # Already known. Refresh display data and re-open it if a previous scan
        # had marked it fixed and the error has come back.
        reopen = row["status"] == "resolved"
        conn.execute(
            """
            UPDATE issues SET
                last_seen_version_id = ?,
                last_scan_id = ?,
                para_index = ?,
                char_offset = ?,
                match_length = ?,
                message = ?,
                context_before = ?,
                context_after = ?,
                suggestions_json = ?,
                in_quote = ?,
                suppressed_reason = ?,
                status = CASE WHEN status = 'resolved' THEN 'open' ELSE status END,
                resolved_at = CASE WHEN status = 'resolved' THEN NULL ELSE resolved_at END
            WHERE id = ?
            """,
            (
                version_id,
                scan_id,
                issue.para_index,
                issue.char_offset,
                issue.match_length,
                issue.message,
                issue.context_before,
                issue.context_after,
                suggestions,
                int(issue.in_quote),
                issue.suppressed_reason,
                row["id"],
            ),
        )
        if reopen and not issue.suppressed:
            new_count += 1

    # Anything open that we did not see this time has been corrected.
    stale = [
        row["id"]
        for fingerprint, row in existing.items()
        if fingerprint not in seen_fingerprints and row["status"] == "open"
    ]
    if stale:
        conn.executemany(
            "UPDATE issues SET status = 'resolved', resolved_at = ? WHERE id = ?",
            [(now, issue_id) for issue_id in stale],
        )

    return new_count, len(stale)


# --- the scan -------------------------------------------------------------


def _target_urls_for_rescan(
    conn: sqlite3.Connection, source_id: int, since_days: Optional[int], open_only: bool
) -> List[tuple]:
    query = """
        SELECT DISTINCT a.url, a.title
        FROM articles a
        {join}
        WHERE a.source_id = ?
        {since}
        ORDER BY a.last_scanned_at IS NULL DESC, a.last_scanned_at ASC
    """
    params: List = [source_id]
    join = (
        "JOIN issues i ON i.article_id = a.id AND i.status = 'open'"
        if open_only
        else ""
    )
    since = ""
    if since_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat(
            timespec="seconds"
        )
        since = "AND a.first_seen_at >= ?"
        params.append(cutoff)

    rows = conn.execute(query.format(join=join, since=since), params).fetchall()
    return [(row["url"], row["title"]) for row in rows]


def scan(
    conn: sqlite3.Connection,
    source_key: str = "wral",
    limit: Optional[int] = None,
    rescan: bool = False,
    open_only: bool = True,
    since_days: Optional[int] = None,
    use_ner: bool = True,
    fetcher: Optional[Fetcher] = None,
    checker: Optional[GrammarChecker] = None,
    on_progress: Optional[Callable[[ArticleResult], None]] = None,
) -> ScanSummary:
    """Run one scan. Does not commit -- the caller does."""
    source: Source = get_source(source_key)
    src_id = db.source_id(conn, source_key)
    kind = "rescan" if rescan else "scan"

    cursor = conn.execute(
        "INSERT INTO scans (source_id, kind, started_at) VALUES (?, ?, ?)",
        (src_id, kind, db.utcnow()),
    )
    scan_id = cursor.lastrowid
    summary = ScanSummary(scan_id=scan_id, source_key=source_key, kind=kind)

    owns_fetcher = fetcher is None
    owns_checker = checker is None
    fetcher = fetcher or Fetcher()
    checker = checker or GrammarChecker()
    filters = FilterChain(
        dictionary=db.load_dictionary(conn),
        suppressed_rules=db.load_suppressed_rules(conn),
        use_ner=use_ner,
    )

    try:
        if rescan:
            targets = _target_urls_for_rescan(conn, src_id, since_days, open_only)
        else:
            targets = [(entry.url, entry.title) for entry in fetcher.feed(source, limit or 50)]

        if limit is not None:
            targets = targets[:limit]
        summary.articles_seen = len(targets)

        for url, title in targets:
            result = _process(
                conn, fetcher, checker, filters, source, src_id, scan_id, url, title
            )
            summary.results.append(result)

            if result.outcome == "checked":
                summary.articles_checked += 1
            elif result.outcome == "unchanged":
                summary.articles_unchanged += 1
            elif result.outcome == "failed":
                summary.articles_failed += 1

            if on_progress:
                on_progress(result)

        summary.issues_new = sum(result.new_issues for result in summary.results)
        summary.issues_resolved = sum(result.resolved_issues for result in summary.results)

        conn.execute(
            """
            UPDATE scans SET
                finished_at = ?, status = 'complete',
                articles_seen = ?, articles_checked = ?, articles_failed = ?,
                articles_unchanged = ?, issues_new = ?, issues_resolved = ?
            WHERE id = ?
            """,
            (
                db.utcnow(),
                summary.articles_seen,
                summary.articles_checked,
                summary.articles_failed,
                summary.articles_unchanged,
                summary.issues_new,
                summary.issues_resolved,
                scan_id,
            ),
        )
    except BaseException as error:
        conn.execute(
            "UPDATE scans SET finished_at = ?, status = 'error', note = ? WHERE id = ?",
            (db.utcnow(), str(error), scan_id),
        )
        raise
    finally:
        if owns_checker:
            checker.close()
        if owns_fetcher:
            fetcher.close()

    return summary


def _process(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    checker: GrammarChecker,
    filters: FilterChain,
    source: Source,
    src_id: int,
    scan_id: int,
    url: str,
    title: Optional[str],
) -> ArticleResult:
    result = ArticleResult(url=url, title=title)
    article_id = upsert_article(conn, src_id, url, title)
    result.article_id = article_id

    row = conn.execute(
        "SELECT http_etag, http_last_modified FROM articles WHERE id = ?", (article_id,)
    ).fetchone()

    fetched = fetcher.get(url, etag=row["http_etag"], last_modified=row["http_last_modified"])

    if fetched.not_modified:
        conn.execute(
            "UPDATE articles SET last_scanned_at = ? WHERE id = ?", (db.utcnow(), article_id)
        )
        result.outcome = "unchanged"
        result.note = "not modified since last scan"
        return result

    if not fetched.ok:
        conn.execute(
            """
            UPDATE articles SET last_scanned_at = ?, extraction_status = 'failed',
                                extraction_note = ? WHERE id = ?
            """,
            (db.utcnow(), fetched.error, article_id),
        )
        result.outcome = "failed"
        result.note = fetched.error
        return result

    article = extract_article(fetched.text, url, source)
    result.title = article.title or title

    conn.execute(
        """
        UPDATE articles SET
            title = COALESCE(?, title),
            byline = COALESCE(?, byline),
            canonical_url = COALESCE(?, canonical_url),
            published_at = COALESCE(?, published_at),
            modified_at = COALESCE(?, modified_at),
            http_etag = ?, http_last_modified = ?,
            last_scanned_at = ?, extraction_status = ?, extraction_note = ?
        WHERE id = ?
        """,
        (
            article.title,
            article.byline,
            article.canonical_url,
            article.published_at,
            article.modified_at,
            fetched.etag,
            fetched.last_modified,
            db.utcnow(),
            article.status,
            article.note,
            article_id,
        ),
    )

    if not article.ok:
        # Do not resolve anything: we did not actually read this article, and
        # silently marking its issues "fixed" would be a lie.
        result.outcome = "failed"
        result.note = article.note
        return result

    previous = latest_version(conn, article_id)
    if previous is not None and previous["content_hash"] == article.content_hash:
        result.outcome = "unchanged"
        result.note = "content identical to last version"
        return result

    version_id = insert_version(conn, article_id, scan_id, article)
    result.issues = run_detectors(article, checker, filters)
    result.new_issues, result.resolved_issues = persist_issues(
        conn, article_id, version_id, scan_id, result.issues
    )
    result.outcome = "checked"
    return result
