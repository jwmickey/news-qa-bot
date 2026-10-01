"""Scan lifecycle tests.

These cover the behaviors the review UI depends on: an issue keeps its identity
across rescans, a correction is detected as a resolution, a dismissal is never
overwritten, and a failed read never fabricates a "fixed" signal.

A stub checker stands in for LanguageTool so the lifecycle is tested
deterministically and without starting a Java server.
"""

import pytest

from news_qa import db
from news_qa.detect.base import SPELLING, Issue
from news_qa.fetch import FeedEntry, FetchResult
from news_qa.fingerprint import context_window, issue_fingerprint
from news_qa.scan import scan

URL = "https://www.wral.com/news/local/county-budget/"

BODY_WITH_TYPO = (
    "Commissioners voted 4-3 to approve the mesure, which raises the property tax "
    "rate by two cents per $100 of assessed value beginning in July."
)
BODY_FIXED = BODY_WITH_TYPO.replace("mesure", "measure")
SECOND_PARAGRAPH = (
    "The budget takes effect July 1 and includes funding for a new elementary "
    "school in the Millbrook area, plus modest raises for county employees."
)


def page(*paragraphs):
    body = "".join(f"<p>{text}</p>" for text in paragraphs)
    return f"<html><body><div class='article-body'>{body}</div></body></html>"


class FakeFetcher:
    """Serves canned pages; mutate ``pages`` between scans to simulate edits."""

    def __init__(self, pages, feed_urls=None):
        self.pages = pages
        self.feed_urls = feed_urls if feed_urls is not None else list(pages)

    def feed(self, source, limit=50):
        return [FeedEntry(title="Story", url=url) for url in self.feed_urls[:limit]]

    def get(self, url, etag=None, last_modified=None):
        content = self.pages.get(url)
        if content is None:
            return FetchResult(url=url, status_code=404, error="HTTP 404")
        return FetchResult(url=url, status_code=200, text=content)

    def close(self):
        pass


class StubChecker:
    """Flags a fixed vocabulary of typos, mimicking LanguageTool's output shape."""

    TYPOS = ("mesure", "aproved", "sies")

    def check(self, article):
        issues = []
        for paragraph in article.paragraphs:
            for typo in self.TYPOS:
                start = paragraph.text.find(typo)
                if start < 0:
                    continue
                before, after = context_window(paragraph.text, start, len(typo))
                issues.append(
                    Issue(
                        kind=SPELLING,
                        rule_id="MORFOLOGIK_RULE_EN_US",
                        category="TYPOS",
                        message="Possible spelling mistake found.",
                        matched_text=typo,
                        context_before=before,
                        context_after=after,
                        para_index=paragraph.index,
                        char_offset=start,
                        match_length=len(typo),
                        fingerprint=issue_fingerprint(
                            SPELLING, "MORFOLOGIK_RULE_EN_US", typo, before, after
                        ),
                    )
                )
        return issues

    def close(self):
        pass


@pytest.fixture
def run(conn):
    """Run a scan against a given set of pages, without NER, and commit."""

    def _run(pages, **kwargs):
        summary = scan(
            conn,
            source_key="wral",
            fetcher=FakeFetcher(pages),
            checker=StubChecker(),
            use_ner=False,
            **kwargs,
        )
        conn.commit()
        return summary

    return _run


def issue_rows(conn, url=URL):
    return conn.execute(
        """
        SELECT i.* FROM issues i
        JOIN articles a ON a.id = i.article_id
        WHERE a.url = ?
        ORDER BY i.id
        """,
        (url,),
    ).fetchall()


# --- first scan -----------------------------------------------------------


def test_first_scan_records_article_version_and_issue(conn, run):
    summary = run({URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)})

    assert summary.articles_checked == 1
    assert summary.issues_new == 1
    assert summary.issues_resolved == 0

    rows = issue_rows(conn)
    assert len(rows) == 1
    assert rows[0]["matched_text"] == "mesure"
    assert rows[0]["status"] == "open"

    versions = conn.execute("SELECT COUNT(*) n FROM article_versions").fetchone()["n"]
    assert versions == 1


def test_scan_row_is_recorded_and_completed(conn, run):
    summary = run({URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)})
    row = conn.execute("SELECT * FROM scans WHERE id = ?", (summary.scan_id,)).fetchone()
    assert row["status"] == "complete"
    assert row["finished_at"]
    assert row["articles_checked"] == 1


# --- rescan: unchanged ----------------------------------------------------


def test_unchanged_content_creates_no_new_version_or_issue(conn, run):
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    run(pages)
    summary = run(pages)

    assert summary.articles_unchanged == 1
    assert summary.articles_checked == 0
    assert conn.execute("SELECT COUNT(*) n FROM article_versions").fetchone()["n"] == 1
    assert len(issue_rows(conn)) == 1
    assert issue_rows(conn)[0]["status"] == "open"


# --- rescan: the fix-detection feature -----------------------------------


def test_correcting_the_typo_resolves_the_issue(conn, run):
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    run(pages)

    pages[URL] = page(BODY_FIXED, SECOND_PARAGRAPH)
    summary = run(pages)

    assert summary.issues_resolved == 1
    row = issue_rows(conn)[0]
    assert row["status"] == "resolved"
    assert row["resolved_at"] is not None


def test_issue_survives_an_unrelated_edit_with_its_identity_intact(conn, run):
    """Adding a paragraph above must not resurrect the issue as new."""
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    run(pages)
    original_id = issue_rows(conn)[0]["id"]

    pages[URL] = page(
        "A crowd of about sixty residents filled the chamber and the overflow room.",
        BODY_WITH_TYPO,
        SECOND_PARAGRAPH,
    )
    summary = run(pages)

    rows = issue_rows(conn)
    assert len(rows) == 1
    assert rows[0]["id"] == original_id
    assert rows[0]["status"] == "open"
    assert summary.issues_new == 0
    assert summary.issues_resolved == 0
    # Position was refreshed even though identity held.
    assert rows[0]["para_index"] == 1


def test_reintroducing_a_typo_reopens_the_resolved_issue(conn, run):
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    run(pages)
    pages[URL] = page(BODY_FIXED, SECOND_PARAGRAPH)
    run(pages)
    assert issue_rows(conn)[0]["status"] == "resolved"

    pages[URL] = page(BODY_WITH_TYPO, SECOND_PARAGRAPH)
    summary = run(pages)

    row = issue_rows(conn)[0]
    assert row["status"] == "open"
    assert row["resolved_at"] is None
    assert summary.issues_new == 1


# --- dismissals are never overwritten ------------------------------------


def test_a_dismissed_issue_stays_dismissed_across_a_rescan(conn, run):
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    run(pages)
    issue_id = issue_rows(conn)[0]["id"]
    conn.execute("UPDATE issues SET status = 'dismissed' WHERE id = ?", (issue_id,))
    conn.commit()

    pages[URL] = page(BODY_WITH_TYPO, "A different closing paragraph for this article entirely.")
    run(pages)

    assert issue_rows(conn)[0]["status"] == "dismissed"


def test_a_dismissed_issue_is_not_marked_resolved_when_it_disappears(conn, run):
    """Your judgement outlives the text; resolution only applies to open issues."""
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    run(pages)
    conn.execute("UPDATE issues SET status = 'dismissed'")
    conn.commit()

    pages[URL] = page(BODY_FIXED, SECOND_PARAGRAPH)
    summary = run(pages)

    assert summary.issues_resolved == 0
    assert issue_rows(conn)[0]["status"] == "dismissed"


# --- failures never fabricate a fix --------------------------------------


def test_a_fetch_failure_resolves_nothing(conn, run):
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    run(pages)

    summary = scan(
        conn,
        source_key="wral",
        fetcher=FakeFetcher({}, feed_urls=[URL]),
        checker=StubChecker(),
        use_ner=False,
    )
    conn.commit()

    assert summary.articles_failed == 1
    assert summary.issues_resolved == 0
    assert issue_rows(conn)[0]["status"] == "open"


def test_an_extraction_failure_resolves_nothing(conn, run):
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    run(pages)

    # A page whose article container has vanished -- a redesign, or a paywall.
    pages[URL] = "<html><body><nav><p>Home News Weather</p></nav></body></html>"
    summary = run(pages)

    assert summary.articles_failed == 1
    assert summary.issues_resolved == 0
    assert issue_rows(conn)[0]["status"] == "open"

    article = conn.execute("SELECT * FROM articles WHERE url = ?", (URL,)).fetchone()
    assert article["extraction_status"] == "failed"
    assert article["extraction_note"]


# --- dictionary suppression through the full path ------------------------


def test_dictionary_term_is_stored_suppressed_not_dropped(conn, run):
    db.add_dictionary_term(conn, "mesure", note="test")
    conn.commit()

    summary = run({URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)})

    rows = issue_rows(conn)
    assert len(rows) == 1, "the issue is kept so the UI can explain the suppression"
    assert rows[0]["suppressed_reason"] == "dictionary"
    assert summary.issues_new == 0
    assert summary.flagged == []


# --- metadata -------------------------------------------------------------


def test_byline_and_title_are_recorded(conn, run):
    html = (
        "<html><head><script type='application/ld+json'>"
        '{"@type":"NewsArticle","headline":"County approves budget",'
        '"author":{"@type":"Person","name":"Dana Whitfield"},'
        '"datePublished":"2026-07-28T14:03:00Z"}'
        "</script></head><body><div class='article-body'>"
        f"<p>{BODY_WITH_TYPO}</p><p>{SECOND_PARAGRAPH}</p>"
        "</div></body></html>"
    )
    run({URL: html})

    row = conn.execute("SELECT * FROM articles WHERE url = ?", (URL,)).fetchone()
    assert row["byline"] == "Dana Whitfield"
    assert row["title"] == "County approves budget"
    assert row["published_at"] == "2026-07-28T14:03:00Z"
