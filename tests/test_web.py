"""Web UI tests.

The point of these is the behaviour the browser adds on top of the CLI: that a
dismissal made over HTTP is the same write the CLI makes, that highlighting
lands on the right characters, that a crash cannot leave a scan stuck
"running", and that nothing is quietly scoped to WRAL.

Reuses the FakeFetcher/StubChecker from test_scan so no Java server or network
is involved.
"""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from news_qa import db, sources
from news_qa.web import deps, highlight, queries
from news_qa.web.app import create_app
from news_qa.web.jobs import JobParams, JobRejected, ScanQueue, reap_orphans

from tests.test_scan import (  # noqa: F401 - shared fakes, no Java or network
    BODY_FIXED,
    URL,
    BODY_WITH_TYPO,
    SECOND_PARAGRAPH,
    FakeFetcher,
    StubChecker,
    page,
)


# A second typo, so the bulk actions have more than one row to act on.
THIRD_PARAGRAPH = (
    "The board aproved the plan after two hours of public comment, with three "
    "residents speaking against the tax rate."
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "web.db"


@pytest.fixture
def seeded(db_path):
    """A database with one scanned article carrying two known typos."""
    conn = db.connect(db_path)
    from news_qa.scan import scan

    scan(
        conn,
        source_key="wral",
        fetcher=FakeFetcher({URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH, THIRD_PARAGRAPH)}),
        checker=StubChecker(),
        use_ner=False,
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def client(db_path):
    app = create_app(db_path=db_path, start_worker=False)
    with TestClient(app) as test_client:
        yield test_client
    deps.set_db_path(None)


PAGES = [
    "/",
    "/sources",
    "/sources/wral",
    "/scans",
    "/issues",
    "/issues?status=all&include_hidden=true",
    "/dictionary",
    "/rules",
    "/health",
    "/jobs/active",
]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render_on_an_empty_database(client, path):
    """An unused install must not present a stack trace on first visit."""
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", PAGES)
def test_pages_render_with_data(seeded, client, path):
    assert client.get(path).status_code == 200


def test_detail_pages_render(seeded, client):
    for path in ("/scans/1", "/issues/1", "/articles/1"):
        assert client.get(path).status_code == 200, path


@pytest.fixture
def edited(db_path):
    """An article scanned twice, with the typo corrected in between."""
    from news_qa.scan import scan

    conn = db.connect(db_path)
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    fetcher = FakeFetcher(pages)
    scan(conn, "wral", fetcher=fetcher, checker=StubChecker(), use_ner=False)
    conn.commit()
    pages[URL] = page(BODY_FIXED, SECOND_PARAGRAPH)
    scan(conn, "wral", fetcher=fetcher, checker=StubChecker(), use_ner=False)
    conn.commit()
    conn.close()
    return db_path


def test_the_version_diff_shows_the_correction(edited, client):
    response = client.get("/articles/1/diff")
    assert response.status_code == 200
    # The old paragraph is removed and the corrected one added.
    assert "mesure" in response.text and "measure" in response.text


def test_a_corrected_issue_reads_as_fixed(edited, client):
    assert "fixed" in client.get("/issues?status=resolved").text


def test_the_diff_needs_two_versions(seeded, client):
    assert client.get("/articles/1/diff").status_code == 404


def test_missing_records_are_not_found_not_errors(client):
    for path in ("/issues/999", "/articles/999", "/scans/999", "/sources/nope"):
        assert client.get(path).status_code == 404, path


def test_dashboard_leads_with_the_open_error(seeded, client):
    body = client.get("/").text
    assert "mesure" in body


# --- triage ----------------------------------------------------------------


def open_issue_id(path):
    conn = db.connect(path)
    issue_id = conn.execute(
        "SELECT id FROM issues WHERE status = 'open' AND kind = 'spelling' ORDER BY id"
    ).fetchone()["id"]
    conn.close()
    return issue_id


def test_dismiss_writes_what_the_cli_writes(seeded, client):
    issue_id = open_issue_id(seeded)
    response = client.post(f"/issues/{issue_id}/dismiss", data={"reason": "brand-name"})
    assert response.status_code in (200, 303)

    conn = db.connect(seeded)
    issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    record = conn.execute(
        "SELECT * FROM dismissals WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    conn.close()

    assert issue["status"] == "dismissed"
    assert record["reason"] == "brand-name"
    assert record["revoked_at"] is None


def test_dismiss_can_add_the_term_to_the_dictionary(seeded, client):
    issue_id = open_issue_id(seeded)
    client.post(
        f"/issues/{issue_id}/dismiss",
        data={"reason": "proper-noun", "add_to_dictionary": "1"},
    )
    conn = db.connect(seeded)
    terms = db.load_dictionary(conn)
    conn.close()
    assert "mesure" in terms


def test_dismiss_says_the_term_was_already_in_the_dictionary(seeded, client):
    """A term another dismissal already added is present, not "unchanged"."""
    issue_id = open_issue_id(seeded)
    conn = db.connect(seeded)
    db.add_dictionary_term(conn, "mesure", note="manual")
    conn.commit()
    conn.close()

    response = client.post(
        f"/issues/{issue_id}/dismiss",
        data={"reason": "not-an-error", "add_to_dictionary": "1"},
        headers={"HX-Request": "true"},
    )
    assert "already in the dictionary" in response.text
    assert "unchanged" not in response.text

    conn = db.connect(seeded)
    terms = db.load_dictionary(conn)
    conn.close()
    assert "mesure" in terms


def test_adding_a_term_hides_the_same_error_elsewhere_without_a_rescan(seeded, client):
    """The point of the dictionary is not having to dismiss a name twice."""
    conn = db.connect(seeded)
    issue = conn.execute(
        "SELECT * FROM issues WHERE kind = 'spelling' AND status = 'open' "
        "AND suppressed_reason IS NULL LIMIT 1"
    ).fetchone()
    # A second article carrying the same misspelling, as a rescan would find.
    twin_id = conn.execute(
        """
        INSERT INTO issues (article_id, fingerprint, kind, rule_id, severity, message,
                            matched_text, context_before, context_after, created_at)
        VALUES (?, 'twin-fp', 'spelling', ?, 'error', ?, ?, ?, ?, ?)
        """,
        (
            issue["article_id"],
            issue["rule_id"],
            issue["message"],
            issue["matched_text"],
            "elsewhere ",
            " entirely",
            db.utcnow(),
        ),
    ).lastrowid
    conn.commit()
    conn.close()

    response = client.post(
        f"/issues/{issue['id']}/dismiss",
        data={"reason": "not-an-error", "add_to_dictionary": "1"},
        headers={"HX-Request": "true"},
    )
    assert "Hid 1 other open issue" in response.text

    conn = db.connect(seeded)
    twin = conn.execute("SELECT * FROM issues WHERE id = ?", (twin_id,)).fetchone()
    conn.close()
    # Hidden the way a scan would hide it -- not dismissed, since the editor
    # never passed judgement on this one.
    assert twin["suppressed_reason"] == "dictionary"
    assert twin["status"] == "open"


def test_manually_added_term_hides_matching_open_issues(seeded, client):
    conn = db.connect(seeded)
    issue_id = conn.execute(
        "SELECT id FROM issues WHERE matched_text = 'mesure' AND status = 'open' LIMIT 1"
    ).fetchone()["id"]
    conn.close()

    client.post("/dictionary/add", data={"term": "mesure"})

    conn = db.connect(seeded)
    row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    conn.close()
    assert row["suppressed_reason"] == "dictionary"


def test_removing_a_term_unhides_what_it_was_hiding(seeded, client):
    conn = db.connect(seeded)
    issue_id = conn.execute(
        "SELECT id FROM issues WHERE matched_text = 'mesure' AND status = 'open' LIMIT 1"
    ).fetchone()["id"]
    conn.close()

    client.post("/dictionary/add", data={"term": "mesure"})
    client.post("/dictionary/remove", data={"term": "mesure"})

    conn = db.connect(seeded)
    row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    conn.close()
    assert row["suppressed_reason"] is None


def test_reopen_restores_the_issue_but_keeps_the_judgement(seeded, client):
    issue_id = open_issue_id(seeded)
    client.post(f"/issues/{issue_id}/dismiss", data={"reason": "wont-fix"})
    client.post(f"/issues/{issue_id}/reopen")

    conn = db.connect(seeded)
    issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    record = conn.execute(
        "SELECT * FROM dismissals WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    conn.close()

    assert issue["status"] == "open"
    # The record of the judgement survives -- it is stamped, never deleted.
    assert record is not None
    assert record["reason"] == "wont-fix"
    assert record["revoked_at"] is not None


def test_htmx_dismiss_returns_only_the_row(seeded, client):
    issue_id = open_issue_id(seeded)
    response = client.post(
        f"/issues/{issue_id}/dismiss",
        data={"reason": "not-an-error"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "<html" not in response.text
    assert f'id="issue-row-{issue_id}"' in response.text
    assert "Restore mark" in response.text


def test_bulk_dismiss(seeded, client):
    conn = db.connect(seeded)
    ids = [row["id"] for row in conn.execute("SELECT id FROM issues WHERE status = 'open'")]
    conn.close()
    assert len(ids) >= 2

    client.post(
        "/issues/bulk",
        data={"issue_ids": ids, "action": "dismiss", "reason": "house-style"},
        follow_redirects=False,
    )
    conn = db.connect(seeded)
    remaining = conn.execute(
        "SELECT COUNT(*) n FROM issues WHERE status = 'open'"
    ).fetchone()["n"]
    conn.close()
    assert remaining == 0


def test_dictionary_and_rules_round_trip(client):
    client.post("/dictionary/add", data={"term": "Fuquay-Varina, GreenWise"})
    # Match a table cell, not the page: the add field's placeholder names these too.
    assert "<td>GreenWise</td>" in client.get("/dictionary").text
    client.post("/dictionary/remove", data={"term": "GreenWise"})
    assert "<td>GreenWise</td>" not in client.get("/dictionary").text
    assert "<td>Fuquay-Varina</td>" in client.get("/dictionary").text

    client.post("/rules/add", data={"rule_id": "UPPERCASE_SENTENCE_START"})
    assert "UPPERCASE_SENTENCE_START" in client.get("/rules").text
    client.post("/rules/remove", data={"rule_id": "UPPERCASE_SENTENCE_START"})


# --- highlighting ----------------------------------------------------------


def issue(**overrides):
    base = {
        "id": 1,
        "para_index": 0,
        "char_offset": 0,
        "match_length": 3,
        "severity": "error",
        "status": "open",
        "kind": "spelling",
        "message": "Possible spelling mistake found.",
        "suppressed_reason": None,
    }
    base.update(overrides)
    return base


def test_highlight_marks_the_right_characters():
    body = [{"index": 0, "text": "The mesure passed."}]
    rendered = highlight.render_paragraphs(body, [issue(char_offset=4, match_length=6)])
    assert rendered[0]["html"] == (
        'The <mark id="issue-1" class="mark mark-error"'
        ' title="Possible spelling mistake found.">mesure</mark> passed.'
    )


def test_highlight_escapes_the_article_text():
    body = [{"index": 0, "text": "a <script> & b"}]
    rendered = highlight.render_paragraphs(body, [])
    assert "<script>" not in rendered[0]["html"]
    assert "&lt;script&gt; &amp; b" in rendered[0]["html"]


def test_two_marks_in_one_paragraph_both_survive():
    body = [{"index": 0, "text": "aaa bbb ccc"}]
    rendered = highlight.render_paragraphs(
        body,
        [
            issue(id=1, char_offset=0, match_length=3),
            issue(id=2, char_offset=8, match_length=3),
        ],
    )
    assert rendered[0]["html"].count("<mark") == 2
    assert rendered[0]["html"].endswith("</mark>")


def test_overlapping_marks_are_dropped_not_nested():
    body = [{"index": 0, "text": "aaa bbb ccc"}]
    rendered = highlight.render_paragraphs(
        body,
        [
            issue(id=1, char_offset=0, match_length=7),
            issue(id=2, char_offset=4, match_length=3),
        ],
    )
    assert rendered[0]["html"].count("<mark") == 1


def test_a_mark_past_the_end_of_the_paragraph_is_skipped():
    """Stale offsets must degrade to plain text, never to a slice error."""
    body = [{"index": 0, "text": "short"}]
    rendered = highlight.render_paragraphs(body, [issue(char_offset=3, match_length=99)])
    assert rendered[0]["html"] == "short"


def test_dismissed_marks_are_dimmed_rather_than_removed():
    body = [{"index": 0, "text": "The mesure passed."}]
    rendered = highlight.render_paragraphs(
        body, [issue(char_offset=4, match_length=6, status="dismissed")]
    )
    assert "mark-dismissed" in rendered[0]["html"]
    assert "mesure" in rendered[0]["html"]


def test_duplicates_are_not_highlighted_but_paired():
    """kind='duplicate' stores paragraph A in matched_text and B in context_after."""
    body = [{"index": 0, "text": "Repeated line."}]
    duplicate = issue(
        kind="duplicate", matched_text="Repeated line.", context_after="Repeated line."
    )
    rendered = highlight.render_paragraphs(body, [duplicate])
    assert "<mark" not in rendered[0]["html"]
    assert highlight.duplicate_pair(duplicate) == ["Repeated line.", "Repeated line."]


def test_suggestions_split_on_commas_not_json():
    assert queries.suggestions({"suggestions_json": "measure,measures"}) == [
        "measure",
        "measures",
    ]
    assert queries.suggestions({"suggestions_json": None}) == []


# --- concurrency -----------------------------------------------------------


def test_pages_load_while_a_scan_holds_the_write_lock(seeded, client):
    """The regression that made every page view fight the scan worker.

    db.connect() runs seed(), which writes. When request connections migrated
    too, every page load wanted the write lock and 500'd mid-scan.
    """
    holder = db.connect(seeded)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE scans SET note = 'scanning' WHERE id = 1")
    try:
        for path in ("/", "/issues", "/jobs/active", "/sources/wral"):
            assert client.get(path).status_code == 200, path
    finally:
        holder.rollback()
        holder.close()


def test_a_dismissal_lands_once_the_scan_commits(seeded, client):
    """A write blocks only for the lock window, then succeeds -- it never 500s."""
    import threading

    issue_id = open_issue_id(seeded)
    holding = threading.Event()

    def hold_then_commit():
        """Stand in for the worker: take the lock, then commit between articles."""
        conn = db.connect(seeded)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE scans SET note = 'scanning' WHERE id = 1")
        holding.set()
        threading.Event().wait(0.4)
        conn.commit()
        conn.close()

    worker = threading.Thread(target=hold_then_commit)
    worker.start()
    assert holding.wait(5)

    response = client.post(f"/issues/{issue_id}/dismiss", data={"reason": "brand-name"})
    worker.join(timeout=10)

    assert response.status_code in (200, 303)
    conn = db.connect(seeded)
    status = conn.execute("SELECT status FROM issues WHERE id = ?", (issue_id,)).fetchone()
    conn.close()
    assert status["status"] == "dismissed"


# --- the queue -------------------------------------------------------------


def test_reap_orphans_clears_a_crashed_run(seeded):
    conn = db.connect(seeded)
    conn.execute("UPDATE scans SET status = 'running', finished_at = NULL WHERE id = 1")
    conn.execute(
        "INSERT INTO scan_jobs (source_id, kind, state, requested_at) "
        "VALUES (1, 'scan', 'running', ?)",
        (db.utcnow(),),
    )
    conn.commit()

    assert reap_orphans(conn) == 1
    assert conn.execute("SELECT status FROM scans WHERE id = 1").fetchone()["status"] == "error"
    assert (
        conn.execute("SELECT state FROM scan_jobs WHERE id = 1").fetchone()["state"] == "error"
    )
    conn.close()


def test_queue_runs_a_scan_end_to_end(db_path):
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    queue = ScanQueue(
        db_path=db_path,
        checker_factory=StubChecker,
        fetcher_factory=lambda: FakeFetcher(pages),
    )
    conn = db.connect(db_path)
    job_id = queue.enqueue(conn, "wral", "scan", JobParams(use_ner=False))

    queue.start()
    queue.stop(timeout=30)

    job = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["state"] == "done", job["error"]
    assert job["scan_id"] is not None
    open_issues = conn.execute(
        "SELECT COUNT(*) n FROM issues WHERE status = 'open'"
    ).fetchone()["n"]
    conn.close()
    assert open_issues >= 1


def test_a_second_identical_job_is_rejected_while_one_is_pending(db_path):
    queue = ScanQueue(db_path=db_path, checker_factory=StubChecker)
    conn = db.connect(db_path)
    queue.enqueue(conn, "wral", "scan")
    with pytest.raises(JobRejected):
        queue.enqueue(conn, "wral", "scan")
    # A different kind against the same source is a different job, and allowed.
    queue.enqueue(conn, "wral", "rescan")
    conn.close()


def test_a_failing_scan_marks_the_job_and_the_scan_without_killing_the_worker(db_path):
    class ExplodingFetcher:
        def feed(self, source, limit=50):
            raise RuntimeError("feed is down")

        def close(self):
            pass

    queue = ScanQueue(
        db_path=db_path, checker_factory=StubChecker, fetcher_factory=ExplodingFetcher
    )
    conn = db.connect(db_path)
    job_id = queue.enqueue(conn, "wral", "scan")
    queue.start()
    queue.stop(timeout=30)

    job = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
    scan_row = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()

    assert job["state"] == "error"
    assert "feed is down" in job["error"]
    assert scan_row["status"] == "error"


def test_triggering_a_scan_from_the_ui_queues_it(client, db_path):
    response = client.post(
        "/sources/wral/scan", data={"limit": "3"}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 200

    conn = db.connect(db_path)
    job = conn.execute("SELECT * FROM scan_jobs ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert job["state"] == "queued"
    assert JobParams.from_json(job["params_json"]).limit == 3


def test_an_unknown_source_cannot_be_triggered(client):
    response = client.post(
        "/sources/nosuch/scan", data={}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert "Unknown source" in response.text


# --- multi-source ----------------------------------------------------------


@pytest.fixture
def two_sources(monkeypatch):
    """Append a second source the way sources.py would."""
    extra = dataclasses.replace(
        sources.SOURCES[0],
        key="gazette",
        name="Gazette",
        feed_url="https://gazette.example/rss",
        homepage="https://gazette.example",
    )
    both = [*sources.SOURCES, extra]
    monkeypatch.setattr(sources, "SOURCES", both)
    monkeypatch.setattr(db, "SOURCES", both)
    return both


def test_every_source_is_listed_and_scannable(two_sources, client):
    body = client.get("/sources").text
    assert "WRAL" in body and "Gazette" in body
    # Each card carries its own trigger, so nothing routes through a default.
    assert "/sources/wral/scan" in body and "/sources/gazette/scan" in body


def test_issue_filters_scope_to_one_source(two_sources, seeded, client):
    assert "mesure" in client.get("/issues?source=wral").text
    assert "mesure" not in client.get("/issues?source=gazette").text


def test_stats_scope_to_one_source(two_sources, seeded, db_path):
    conn = db.connect(db_path)
    assert queries.stats(conn, source="wral")["open"] >= 1
    assert queries.stats(conn, source="gazette")["open"] == 0
    conn.close()
