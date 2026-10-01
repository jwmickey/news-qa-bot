"""Report rendering tests."""

import pytest

from news_qa import report as report_module
from tests.test_scan import (  # noqa: F401 - fixtures and helpers
    SECOND_PARAGRAPH,
    URL,
    BODY_WITH_TYPO,
    FakeFetcher,
    StubChecker,
    page,
    run,
)


@pytest.fixture
def scanned(conn, run):  # noqa: F811
    summary = run({URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)})
    return conn, summary


def test_report_groups_issues_by_article(scanned):
    conn, summary = scanned
    built = report_module.build_report(conn, summary.scan_id)
    assert built is not None
    assert len(built.articles) == 1
    assert built.issue_count == 1
    assert built.articles[0].url == URL


def test_text_report_contains_the_error_and_the_link(scanned):
    conn, summary = scanned
    text = report_module.render_text(report_module.build_report(conn, summary.scan_id))
    assert "mesure" in text
    assert URL in text
    assert "scan #" in text


def test_html_report_highlights_the_match_and_escapes_content(scanned):
    conn, summary = scanned
    html = report_module.render_html(report_module.build_report(conn, summary.scan_id))
    assert "<mark" in html
    assert "mesure" in html
    assert f"href='{URL}'" in html
    assert "<script" not in html


def test_report_omits_suppressed_issues_by_default(conn, run):  # noqa: F811
    from news_qa import db

    db.add_dictionary_term(conn, "mesure")
    conn.commit()
    summary = run({URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)})

    built = report_module.build_report(conn, summary.scan_id)
    assert built.issue_count == 0
    assert "good day" in report_module.render_text(built)

    with_hidden = report_module.build_report(conn, summary.scan_id, include_suppressed=True)
    assert with_hidden.issue_count == 1


def test_empty_database_yields_no_report(conn):
    assert report_module.build_report(conn) is None


REVISED_CLOSING = (
    "The board will revisit the schedule at its September meeting, when the "
    "finance director is expected to present updated revenue projections for "
    "the coming fiscal year and the capital plan."
)


def test_report_defaults_to_the_latest_scan(conn, run):  # noqa: F811
    run({URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)})
    second = run({URL: page(BODY_WITH_TYPO, REVISED_CLOSING)})
    assert second.articles_checked == 1, "the edit must be a real re-check"
    assert report_module.build_report(conn).scan_id == second.scan_id


def test_default_skips_a_no_op_rescan(conn, run):  # noqa: F811
    """A rescan that found nothing changed must not render an empty report."""
    pages = {URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)}
    first = run(pages)
    noop = run(pages)  # everything unchanged

    assert noop.articles_checked == 0
    built = report_module.build_report(conn)
    assert built.scan_id == first.scan_id
    assert built.issue_count == 1


def test_all_open_ignores_scan_scope(conn, run):  # noqa: F811
    other_url = "https://www.wral.com/news/local/second-story/"
    run({URL: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)})
    run({other_url: page(BODY_WITH_TYPO, SECOND_PARAGRAPH)})

    scoped = report_module.build_report(conn)
    assert len(scoped.articles) == 1

    everything = report_module.build_report(conn, all_open=True)
    assert len(everything.articles) == 2
    assert everything.issue_count == 2
