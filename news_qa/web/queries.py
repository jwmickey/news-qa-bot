"""Read queries for the UI, returning plain dicts.

Kept apart from the routes so the SQL is reviewable in one place, and so the
templates never see a ``sqlite3.Row`` (which silently returns None for a
misspelled column instead of raising).
"""

import json
import sqlite3
from typing import Any, Dict, List, Optional

ISSUE_STATUSES = ("open", "dismissed", "resolved")
SEVERITIES = ("error", "style")
KINDS = ("spelling", "grammar", "style", "duplicate")

DISMISS_REASONS = (
    "not-an-error",
    "proper-noun",
    "brand-name",
    "quoted-source",
    "house-style",
    "wont-fix",
)


def rows(cursor) -> List[Dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def one(cursor) -> Optional[Dict[str, Any]]:
    row = cursor.fetchone()
    return dict(row) if row else None


def suggestions(issue: Dict[str, Any], limit: int = 6) -> List[str]:
    """`suggestions_json` is a comma-joined string, not JSON. See report.py."""
    raw = issue.get("suggestions_json")
    return [part for part in raw.split(",") if part][:limit] if raw else []


# --- sources ---------------------------------------------------------------


def list_sources(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Every source with its rollups. One row per source even with no activity."""
    return rows(
        conn.execute(
            """
            SELECT
                s.*,
                (SELECT COUNT(*) FROM articles a WHERE a.source_id = s.id) AS articles,
                (SELECT COUNT(*) FROM issues i JOIN articles a ON a.id = i.article_id
                  WHERE a.source_id = s.id AND i.status = 'open'
                    AND i.suppressed_reason IS NULL) AS open_issues,
                (SELECT COUNT(*) FROM issues i JOIN articles a ON a.id = i.article_id
                  WHERE a.source_id = s.id AND i.status = 'open' AND i.severity = 'error'
                    AND i.suppressed_reason IS NULL) AS open_errors,
                (SELECT COUNT(*) FROM issues i JOIN articles a ON a.id = i.article_id
                  WHERE a.source_id = s.id AND i.status = 'dismissed') AS dismissed_issues,
                (SELECT COUNT(*) FROM issues i JOIN articles a ON a.id = i.article_id
                  WHERE a.source_id = s.id AND i.status = 'resolved') AS resolved_issues,
                (SELECT started_at FROM scans WHERE source_id = s.id
                  ORDER BY id DESC LIMIT 1) AS last_scan_at,
                (SELECT status FROM scans WHERE source_id = s.id
                  ORDER BY id DESC LIMIT 1) AS last_scan_status,
                (SELECT id FROM scans WHERE source_id = s.id
                  ORDER BY id DESC LIMIT 1) AS last_scan_id
            FROM sources s
            ORDER BY s.active DESC, s.name COLLATE NOCASE
            """
        )
    )


def get_source(conn: sqlite3.Connection, key: str) -> Optional[Dict[str, Any]]:
    for source in list_sources(conn):
        if source["key"] == key:
            return source
    return None


# --- jobs ------------------------------------------------------------------


def active_jobs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return rows(
        conn.execute(
            """
            SELECT j.*, s.key AS source_key, s.name AS source_name
            FROM scan_jobs j JOIN sources s ON s.id = j.source_id
            WHERE j.state IN ('queued', 'running')
            ORDER BY j.id
            """
        )
    )


def recent_jobs(conn: sqlite3.Connection, limit: int = 8) -> List[Dict[str, Any]]:
    return rows(
        conn.execute(
            """
            SELECT j.*, s.key AS source_key, s.name AS source_name
            FROM scan_jobs j JOIN sources s ON s.id = j.source_id
            WHERE j.state IN ('done', 'error')
            ORDER BY j.id DESC LIMIT ?
            """,
            (limit,),
        )
    )


def job(conn: sqlite3.Connection, job_id: int) -> Optional[Dict[str, Any]]:
    return one(
        conn.execute(
            "SELECT j.*, s.key AS source_key, s.name AS source_name "
            "FROM scan_jobs j JOIN sources s ON s.id = j.source_id WHERE j.id = ?",
            (job_id,),
        )
    )


# --- scans -----------------------------------------------------------------


def list_scans(
    conn: sqlite3.Connection, source: Optional[str] = None, limit: int = 50, offset: int = 0
) -> List[Dict[str, Any]]:
    clause, params = ("AND s.key = ?", [source]) if source else ("", [])
    return rows(
        conn.execute(
            f"""
            SELECT sc.*, s.key AS source_key, s.name AS source_name
            FROM scans sc LEFT JOIN sources s ON s.id = sc.source_id
            WHERE 1=1 {clause}
            ORDER BY sc.id DESC LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
    )


def get_scan(conn: sqlite3.Connection, scan_id: int) -> Optional[Dict[str, Any]]:
    return one(
        conn.execute(
            "SELECT sc.*, s.key AS source_key, s.name AS source_name "
            "FROM scans sc LEFT JOIN sources s ON s.id = sc.source_id WHERE sc.id = ?",
            (scan_id,),
        )
    )


def scan_articles(conn: sqlite3.Connection, scan_id: int) -> List[Dict[str, Any]]:
    """Articles this scan touched, with how many issues it left open on each."""
    return rows(
        conn.execute(
            """
            SELECT a.id, a.url, a.title, a.byline, a.extraction_status, a.extraction_note,
                   COUNT(i.id) FILTER (WHERE i.status = 'open'
                                         AND i.suppressed_reason IS NULL) AS open_issues
            FROM articles a
            JOIN article_versions v ON v.article_id = a.id AND v.scan_id = ?
            LEFT JOIN issues i ON i.article_id = a.id
            GROUP BY a.id
            ORDER BY open_issues DESC, a.title
            """,
            (scan_id,),
        )
    )


# --- issues ----------------------------------------------------------------


def _issue_filters(
    source: Optional[str],
    status: Optional[str],
    severity: Optional[str],
    kind: Optional[str],
    rule_id: Optional[str],
    include_hidden: bool,
    query: Optional[str],
    scan_id: Optional[int],
    article_id: Optional[int],
):
    clauses: List[str] = ["1=1"]
    params: List[Any] = []
    if source:
        clauses.append("s.key = ?")
        params.append(source)
    if status:
        clauses.append("i.status = ?")
        params.append(status)
    if severity:
        clauses.append("i.severity = ?")
        params.append(severity)
    if kind:
        clauses.append("i.kind = ?")
        params.append(kind)
    if rule_id:
        clauses.append("i.rule_id = ?")
        params.append(rule_id)
    if not include_hidden:
        clauses.append("i.suppressed_reason IS NULL")
    if scan_id:
        clauses.append("i.last_scan_id = ?")
        params.append(scan_id)
    if article_id:
        clauses.append("i.article_id = ?")
        params.append(article_id)
    if query:
        clauses.append(
            "(i.matched_text LIKE ? OR i.message LIKE ? OR a.title LIKE ?"
            " OR i.context_before LIKE ? OR i.context_after LIKE ?)"
        )
        params.extend([f"%{query}%"] * 5)
    return " AND ".join(clauses), params


ISSUE_SELECT = """
    SELECT i.*, a.url, a.title, a.byline, a.id AS article_id,
           s.key AS source_key, s.name AS source_name,
           d.reason AS dismiss_reason, d.note AS dismiss_note, d.created_at AS dismissed_at
    FROM issues i
    JOIN articles a ON a.id = i.article_id
    JOIN sources s ON s.id = a.source_id
    LEFT JOIN dismissals d ON d.id = (
        SELECT id FROM dismissals WHERE issue_id = i.id AND revoked_at IS NULL
        ORDER BY id DESC LIMIT 1
    )
"""


def list_issues(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
    status: Optional[str] = "open",
    severity: Optional[str] = None,
    kind: Optional[str] = None,
    rule_id: Optional[str] = None,
    include_hidden: bool = False,
    query: Optional[str] = None,
    scan_id: Optional[int] = None,
    article_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
):
    where, params = _issue_filters(
        source, status, severity, kind, rule_id, include_hidden, query, scan_id, article_id
    )
    total = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM issues i
        JOIN articles a ON a.id = i.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE {where}
        """,
        params,
    ).fetchone()["n"]
    found = rows(
        conn.execute(
            f"{ISSUE_SELECT} WHERE {where} ORDER BY a.id, i.para_index, i.char_offset, i.id "
            "LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
    )
    return found, total


def get_issue(conn: sqlite3.Connection, issue_id: int) -> Optional[Dict[str, Any]]:
    return one(conn.execute(f"{ISSUE_SELECT} WHERE i.id = ?", (issue_id,)))


def issue_history(conn: sqlite3.Connection, issue_id: int) -> List[Dict[str, Any]]:
    return rows(
        conn.execute(
            "SELECT * FROM dismissals WHERE issue_id = ? ORDER BY id DESC", (issue_id,)
        )
    )


def group_by_article(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group a flat issue list for display, preserving order."""
    grouped: Dict[int, Dict[str, Any]] = {}
    for issue in issues:
        article = grouped.setdefault(
            issue["article_id"],
            {
                "article_id": issue["article_id"],
                "url": issue["url"],
                "title": issue["title"] or issue["url"],
                "byline": issue["byline"],
                "source_key": issue["source_key"],
                "source_name": issue["source_name"],
                "issues": [],
            },
        )
        article["issues"].append(issue)
    return list(grouped.values())


# --- articles --------------------------------------------------------------


def get_article(conn: sqlite3.Connection, article_id: int) -> Optional[Dict[str, Any]]:
    return one(
        conn.execute(
            "SELECT a.*, s.key AS source_key, s.name AS source_name "
            "FROM articles a JOIN sources s ON s.id = a.source_id WHERE a.id = ?",
            (article_id,),
        )
    )


def article_versions(conn: sqlite3.Connection, article_id: int) -> List[Dict[str, Any]]:
    return rows(
        conn.execute(
            "SELECT id, scan_id, content_hash, paragraph_count, title, byline, fetched_at "
            "FROM article_versions WHERE article_id = ? ORDER BY id DESC",
            (article_id,),
        )
    )


def version_body(conn: sqlite3.Connection, version_id: int) -> List[Dict[str, Any]]:
    row = conn.execute(
        "SELECT body_json FROM article_versions WHERE id = ?", (version_id,)
    ).fetchone()
    return json.loads(row["body_json"]) if row else []


def latest_version_id(conn: sqlite3.Connection, article_id: int) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM article_versions WHERE article_id = ? ORDER BY id DESC LIMIT 1",
        (article_id,),
    ).fetchone()
    return row["id"] if row else None


# --- settings --------------------------------------------------------------


def dictionary_terms(conn: sqlite3.Connection, query: Optional[str] = None):
    clause, params = ("WHERE term LIKE ?", [f"%{query}%"]) if query else ("", [])
    return rows(
        conn.execute(
            f"SELECT * FROM dictionary {clause} ORDER BY term COLLATE NOCASE", params
        )
    )


def suppressed_rules(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return rows(
        conn.execute(
            """
            SELECT p.*, (SELECT COUNT(*) FROM issues i WHERE i.rule_id = p.rule_id) AS hits
            FROM suppressions p ORDER BY p.rule_id
            """
        )
    )


# --- stats -----------------------------------------------------------------


def stats(conn: sqlite3.Connection, source: Optional[str] = None) -> Dict[str, Any]:
    """Counts and time-to-fix. Mirrors cli.cmd_stats so both tell one story."""
    clause, params = ("AND s.key = ?", [source]) if source else ("", [])
    scope = f"""
        FROM issues i
        JOIN articles a ON a.id = i.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE i.suppressed_reason IS NULL {clause}
    """

    by_status = {
        row["status"]: row["n"]
        for row in conn.execute(f"SELECT i.status, COUNT(*) n {scope} GROUP BY i.status", params)
    }
    fixed = dict(
        conn.execute(
            f"""
            SELECT COUNT(*) n,
                   AVG(julianday(i.resolved_at) - julianday(i.created_at)) avg_days,
                   MAX(julianday(i.resolved_at) - julianday(i.created_at)) max_days
            {scope} AND i.status = 'resolved' AND i.resolved_at IS NOT NULL
            """,
            params,
        ).fetchone()
    )
    top_rules = rows(
        conn.execute(
            f"SELECT i.rule_id, COUNT(*) n {scope} AND i.status = 'open' "
            "GROUP BY i.rule_id ORDER BY n DESC LIMIT 8",
            params,
        )
    )
    hidden = rows(
        conn.execute(
            f"""
            SELECT i.suppressed_reason, COUNT(*) n
            FROM issues i
            JOIN articles a ON a.id = i.article_id
            JOIN sources s ON s.id = a.source_id
            WHERE i.suppressed_reason IS NOT NULL {clause}
            GROUP BY i.suppressed_reason ORDER BY n DESC
            """,
            params,
        )
    )
    by_kind = rows(
        conn.execute(
            f"SELECT i.kind, COUNT(*) n {scope} AND i.status = 'open' "
            "GROUP BY i.kind ORDER BY n DESC",
            params,
        )
    )

    return {
        "by_status": by_status,
        "open": by_status.get("open", 0),
        "dismissed": by_status.get("dismissed", 0),
        "resolved": by_status.get("resolved", 0),
        "fixed": fixed,
        "top_rules": top_rules,
        "hidden": hidden,
        "by_kind": by_kind,
    }
