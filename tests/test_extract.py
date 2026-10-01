"""Extraction tests.

The first test in this file is the regression guard for the bug that motivated
the rewrite: stripping an <a> tag used to weld its text to the surrounding
words, manufacturing a spelling error for every link in every article.
"""

import pytest

from news_qa.extract import (
    block_to_text,
    clean_text,
    extract_article,
    tidy_spacing,
)
from bs4 import BeautifulSoup


def _soup(html):
    return BeautifulSoup(html, "lxml")


# --- the spacing bug ------------------------------------------------------


def test_stripped_link_keeps_surrounding_spaces():
    element = _soup("<p>See the <a href='/r'>county report</a> for details.</p>").p
    assert block_to_text(element) == "See the county report for details."


def test_no_words_are_welded_together_anywhere(fixture_html, source):
    """No output token may contain a lowercase->uppercase seam like 'thecounty Report'."""
    article = extract_article(fixture_html("linky_article.html"), "https://example.com/a", source)
    for word in article.body_text.split():
        assert "  " not in word
    # A word ending in one sentence and starting the next, e.g. "details.The"
    assert not any(
        part and part[0].isupper()
        for word in article.body_text.split()
        for part in word.split(".")[1:]
        if part.isalpha()
    )


def test_inline_emphasis_does_not_lose_spaces():
    element = _soup("<p>It took <em>three</em> hours of debate.</p>").p
    assert block_to_text(element) == "It took three hours of debate."


def test_br_becomes_a_space_not_a_join():
    element = _soup("<p>She voted yes.<br>She explained why.</p>").p
    assert block_to_text(element) == "She voted yes. She explained why."


# --- punctuation repair ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The board 's decision", "The board's decision"),
        ("the clerk , who voted", "the clerk, who voted"),
        ("said Smith .", "said Smith."),
        ("( see page 4 )", "(see page 4)"),
        ("raised by 5 % this year", "raised by 5% this year"),
        ("it doesn 't matter", "it doesn't matter"),
        ("a well - known figure", "a well-known figure"),
    ],
)
def test_tidy_spacing_repairs_separator_artifacts(raw, expected):
    assert tidy_spacing(raw) == expected


def test_tidy_spacing_never_joins_two_words():
    assert tidy_spacing("the county report for details") == "the county report for details"


def test_clean_text_normalizes_nbsp_and_zero_width():
    assert clean_text("Renata Okonkwo​ spoke") == "Renata Okonkwo spoke"


# --- structure and boilerplate -------------------------------------------


def test_boilerplate_containers_are_excluded(fixture_html, source):
    article = extract_article(fixture_html("linky_article.html"), "https://example.com/a", source)
    body = article.body_text.lower()
    assert "privacy policy" not in body  # footer
    assert "daily newsletter" not in body  # promo
    assert "related:" not in body  # aside
    assert "weather sports traffic" not in body  # nav
    assert "all rights reserved" not in body  # in-body boilerplate


def test_blockquote_children_are_taken_once(fixture_html, source):
    article = extract_article(fixture_html("linky_article.html"), "https://example.com/a", source)
    quote = "We looked at every option"
    assert sum(quote in paragraph.text for paragraph in article.paragraphs) == 1


def test_paragraphs_are_indexed_and_body_is_reproducible(fixture_html, source):
    article = extract_article(fixture_html("linky_article.html"), "https://example.com/a", source)
    assert [p.index for p in article.paragraphs] == list(range(len(article.paragraphs)))
    assert article.body_text == "\n\n".join(p.text for p in article.paragraphs)
    again = extract_article(fixture_html("linky_article.html"), "https://example.com/a", source)
    assert again.content_hash == article.content_hash


def test_metadata_from_meta_tags(fixture_html, source):
    article = extract_article(fixture_html("linky_article.html"), "https://example.com/a", source)
    assert article.title == "County approves budget"
    assert article.canonical_url == "https://example.com/news/county-budget"
    assert article.published_at == "2026-07-28T14:03:00Z"


# --- JSON-LD path ---------------------------------------------------------


def test_json_ld_body_is_preferred(fixture_html, source):
    article = extract_article(fixture_html("jsonld_article.html"), "https://example.com/b", source)
    assert article.method == "json-ld"
    assert article.ok
    assert len(article.paragraphs) == 3
    assert "Fallback body text" not in article.body_text


def test_json_ld_authors_become_a_byline(fixture_html, source):
    article = extract_article(fixture_html("jsonld_article.html"), "https://example.com/b", source)
    assert article.byline == "Dana Whitfield, Marcus Ruiz"
    assert article.title == "Crews assess storm damage across the county"
    assert article.modified_at == "2026-07-29T11:40:00Z"


# --- failure instead of garbage ------------------------------------------


def test_page_without_an_article_body_fails_loudly(fixture_html, source):
    article = extract_article(fixture_html("no_body_article.html"), "https://example.com/c", source)
    assert not article.ok
    assert article.status == "failed"
    assert article.note
    # The old code would have scraped nav and footer text here.
    assert "Privacy policy" not in article.body_text
