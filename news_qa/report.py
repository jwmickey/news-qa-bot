"""Render a scan from the database.

Reports are built from stored rows rather than from an in-memory scan result,
so any past scan can be re-rendered at any time -- which the old email-only
pipeline could not do.
"""

import html
import smtplib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import List, Optional

from . import config

MAX_ISSUES_PER_ARTICLE = 15


@dataclass
class ReportArticle:
    article_id: int
    url: str
    title: str
    byline: Optional[str]
    issues: List[sqlite3.Row]


@dataclass
class Report:
    scan_id: int
    started_at: str
    kind: str
    articles_checked: int
    articles_failed: int
    articles_unchanged: int
    issues_new: int
    issues_resolved: int
    articles: List[ReportArticle]

    @property
    def issue_count(self) -> int:
        return sum(len(article.issues) for article in self.articles)

    @property
    def date_label(self) -> str:
        try:
            return datetime.fromisoformat(self.started_at).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return self.started_at


def latest_scan_id(conn: sqlite3.Connection) -> Optional[int]:
    """The most recent scan that actually checked something.

    A rescan where every article was unchanged produces no findings, and
    defaulting to it would render an empty report right after a daily run that
    did find things. Falls back to the newest scan if none checked anything.
    """
    row = conn.execute(
        "SELECT id FROM scans WHERE articles_checked > 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row["id"]
    row = conn.execute("SELECT id FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def build_report(
    conn: sqlite3.Connection,
    scan_id: Optional[int] = None,
    severity: Optional[str] = "error",
    include_suppressed: bool = False,
    all_open: bool = False,
) -> Optional[Report]:
    """Collect open issues grouped by article.

    Scoped to one scan by default; ``all_open`` widens it to every open issue
    in the database, which is the useful view when triaging a backlog.
    """
    if scan_id is None:
        scan_id = latest_scan_id(conn)
    if scan_id is None:
        return None

    scan_row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if scan_row is None:
        return None

    filters = ["i.status = 'open'"]
    params: List = []
    if not all_open:
        filters.append("i.last_scan_id = ?")
        params.append(scan_id)
    if not include_suppressed:
        filters.append("i.suppressed_reason IS NULL")
    if severity:
        filters.append("i.severity = ?")
        params.append(severity)

    rows = conn.execute(
        f"""
        SELECT i.*, a.url, a.title, a.byline, a.id AS a_id
        FROM issues i
        JOIN articles a ON a.id = i.article_id
        WHERE {' AND '.join(filters)}
        ORDER BY a.id, i.para_index, i.char_offset
        """,
        params,
    ).fetchall()

    grouped: dict = {}
    for row in rows:
        article = grouped.get(row["a_id"])
        if article is None:
            article = ReportArticle(
                article_id=row["a_id"],
                url=row["url"],
                title=row["title"] or row["url"],
                byline=row["byline"],
                issues=[],
            )
            grouped[row["a_id"]] = article
        article.issues.append(row)

    return Report(
        scan_id=scan_id,
        started_at=scan_row["started_at"],
        kind=scan_row["kind"],
        articles_checked=scan_row["articles_checked"],
        articles_failed=scan_row["articles_failed"],
        articles_unchanged=scan_row["articles_unchanged"],
        issues_new=scan_row["issues_new"],
        issues_resolved=scan_row["issues_resolved"],
        articles=list(grouped.values()),
    )


def _excerpt(row: sqlite3.Row) -> str:
    """Show the match in context.

    No padding around the brackets: the surrounding text is reproduced exactly
    so that spacing problems in the article stay visible rather than being
    masked by the report's own formatting.
    """
    before = row["context_before"] or ""
    after = row["context_after"] or ""
    return f"...{before}[{row['matched_text']}]{after}..."


def _is_duplicate(row: sqlite3.Row) -> bool:
    return row["kind"] == "duplicate"


def render_text(report: Report) -> str:
    header = f"News QA report — scan #{report.scan_id} — {report.date_label}"
    lines = [header, "=" * len(header), ""]
    lines.append(
        f"Checked {report.articles_checked} articles "
        f"({report.articles_unchanged} unchanged, {report.articles_failed} failed). "
        f"{report.issues_new} new issues, {report.issues_resolved} resolved in this scan."
    )

    if not report.articles:
        lines.append("")
        lines.append("No open issues. The newsroom is having a good day.")
        return "\n".join(lines)

    lines.append(f"Issues in {len(report.articles)} articles:")

    for article in report.articles:
        lines.append("")
        lines.append(f"* {article.title}")
        if article.byline:
            lines.append(f"  by {article.byline}")
        lines.append(f"  {article.url}")

        for row in article.issues[:MAX_ISSUES_PER_ARTICLE]:
            marker = " (in quoted speech)" if row["in_quote"] else ""
            lines.append(f"  - [{row['kind']}/{row['rule_id']}] {row['message']}{marker}")

            if _is_duplicate(row):
                # matched_text and context_after hold the two paragraphs.
                lines.append(f'      A: "{row["matched_text"]}"')
                if row["context_after"]:
                    lines.append(f'      B: "{row["context_after"]}"')
                continue

            lines.append(f"      {_excerpt(row)}")
            if row["suggestions_json"]:
                suggestions = row["suggestions_json"].split(",")[:4]
                lines.append(f"      suggested: {', '.join(suggestions)}")

        remaining = len(article.issues) - MAX_ISSUES_PER_ARTICLE
        if remaining > 0:
            lines.append(f"  ... and {remaining} more")

    return "\n".join(lines)


def render_html(report: Report) -> str:
    def esc(value) -> str:
        return html.escape(str(value if value is not None else ""))

    parts = [
        "<html><body style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        'line-height:1.5;color:#1a1a1a;max-width:46rem;margin:0 auto;padding:1.5rem">',
        f"<h1 style='font-size:1.25rem'>News QA report — scan #{report.scan_id}</h1>",
        f"<p style='color:#555'>{esc(report.date_label)}</p>",
        f"<p>Checked {report.articles_checked} articles "
        f"({report.articles_unchanged} unchanged, {report.articles_failed} failed). "
        f"<strong>{report.issues_new}</strong> new, "
        f"<strong>{report.issues_resolved}</strong> resolved.</p>",
    ]

    if not report.articles:
        parts.append("<p>No open issues. The newsroom is having a good day.</p></body></html>")
        return "".join(parts)

    for article in report.articles:
        parts.append("<hr style='border:none;border-top:1px solid #ddd;margin:1.5rem 0'>")
        parts.append(
            f"<h2 style='font-size:1rem;margin-bottom:.2rem'>"
            f"<a href='{esc(article.url)}' style='color:#0b5cad'>{esc(article.title)}</a></h2>"
        )
        if article.byline:
            parts.append(f"<p style='color:#666;margin:.2rem 0'>by {esc(article.byline)}</p>")

        parts.append("<ul style='padding-left:1.1rem'>")
        for row in article.issues[:MAX_ISSUES_PER_ARTICLE]:
            marker = (
                " <em style='color:#888'>(in quoted speech)</em>" if row["in_quote"] else ""
            )
            if _is_duplicate(row):
                body = (
                    f"<blockquote style='margin:.3rem 0;padding-left:.6rem;"
                    f"border-left:3px solid #ddd;color:#444'>{esc(row['matched_text'])}"
                    f"</blockquote>"
                )
                if row["context_after"]:
                    body += (
                        f"<blockquote style='margin:.3rem 0;padding-left:.6rem;"
                        f"border-left:3px solid #ddd;color:#444'>"
                        f"{esc(row['context_after'])}</blockquote>"
                    )
            else:
                body = (
                    f"<code style='background:#f4f4f4;padding:.1rem .3rem'>"
                    f"{esc(row['context_before'])}"
                    f"<mark style='background:#ffe08a'>{esc(row['matched_text'])}</mark>"
                    f"{esc(row['context_after'])}</code>"
                )

            parts.append(
                f"<li style='margin-bottom:.6rem'><strong>{esc(row['message'])}</strong>"
                f"{marker}<br>{body}"
                f"<br><span style='color:#888;font-size:.85em'>{esc(row['rule_id'])}</span></li>"
            )
        parts.append("</ul>")

        remaining = len(article.issues) - MAX_ISSUES_PER_ARTICLE
        if remaining > 0:
            parts.append(f"<p style='color:#666'>... and {remaining} more</p>")

    parts.append("</body></html>")
    return "".join(parts)


def send_email(report: Report, subject: Optional[str] = None) -> None:
    """Send the report over SMTP. Raises if email is not configured."""
    if not config.EMAIL_ENABLED:
        raise RuntimeError(
            "Email is not configured; set EMAIL_FROM, EMAIL_TO, and EMAIL_PASSWORD."
        )

    message = EmailMessage()
    message["Subject"] = subject or f"News QA report — {report.date_label}"
    message["From"] = config.EMAIL_FROM
    message["To"] = config.EMAIL_TO
    message.set_content(render_text(report))
    message.add_alternative(render_html(report), subtype="html")

    with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
        server.starttls()
        server.login(config.EMAIL_FROM, config.EMAIL_PASSWORD)
        server.send_message(message)
