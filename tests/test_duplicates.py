"""Duplicate-paragraph detection tests."""

from news_qa.detect.duplicates import find_duplicates
from news_qa.extract import ExtractedArticle, Paragraph

LONG = (
    "Commissioners voted 4-3 to approve the measure, which raises the property tax "
    "rate by two cents per $100 of assessed value beginning in July of next year."
)
OTHER = (
    "The budget takes effect July 1 and includes funding for a new elementary school "
    "in the Millbrook area, plus modest raises for most county employees."
)


def make_article(*texts):
    return ExtractedArticle(
        url="https://example.com/a",
        paragraphs=[Paragraph(index=i, text=t) for i, t in enumerate(texts)],
    )


def test_exact_repeat_is_reported():
    issues = find_duplicates(make_article(LONG, OTHER, LONG))
    exact = [i for i in issues if i.rule_id == "DUPLICATE_PARAGRAPH"]
    assert len(exact) == 1
    assert "appears 2 times" in exact[0].message


def test_near_duplicate_is_reported():
    tweaked = LONG.replace("beginning in July of next year", "beginning in August of next year")
    issues = find_duplicates(make_article(LONG, OTHER, tweaked))
    fuzzy = [i for i in issues if i.rule_id == "NEAR_DUPLICATE_PARAGRAPH"]
    assert len(fuzzy) == 1
    assert "similar" in fuzzy[0].message


def test_distinct_paragraphs_produce_nothing():
    assert find_duplicates(make_article(LONG, OTHER)) == []


def test_short_formulaic_lines_are_not_near_duplicates():
    """A 7-day forecast is not an editing mistake.

    These lines are legitimately similar; flagging them buried the real
    findings on WRAL weather articles.
    """
    forecast = make_article(
        "Sunday: Scattered showers and some storms. Highs in the middle 80s.",
        "Monday: Scattered showers and storms. Highs in the mid-to-upper 80s.",
        "Wednesday: Scattered showers and storms. Highs in the mid-to-upper 80s.",
    )
    fuzzy = [i for i in find_duplicates(forecast) if i.rule_id == "NEAR_DUPLICATE_PARAGRAPH"]
    assert fuzzy == []


def test_short_lines_repeated_verbatim_are_still_reported():
    """Exact repeats keep the lower word threshold -- that is a real mistake."""
    line = "The meeting was held at the county courthouse on Tuesday evening."
    issues = find_duplicates(make_article(line, OTHER, line))
    assert [i.rule_id for i in issues] == ["DUPLICATE_PARAGRAPH"]


def test_fingerprints_are_stable_when_paragraphs_move():
    """The finding must survive the duplicated pair shifting position."""
    filler = "A crowd of about sixty residents filled the chamber and the overflow room."
    before = find_duplicates(make_article(LONG, OTHER, LONG))
    after = find_duplicates(make_article(filler, LONG, OTHER, LONG))

    assert len(before) == len(after) == 1
    assert before[0].fingerprint == after[0].fingerprint
    # The reported position moved even though the identity did not.
    assert before[0].para_index != after[0].para_index
