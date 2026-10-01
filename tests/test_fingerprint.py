"""Fingerprint stability tests.

These guard the two behaviors the review UI depends on: a dismissal must
survive a rescan, and a genuine correction must be detectable as the
disappearance of a fingerprint.
"""

from news_qa.fingerprint import (
    PARAGRAPH_SEPARATOR,
    context_window,
    duplicate_fingerprint,
    issue_fingerprint,
    locate_offset,
)
from news_qa.textnorm import normalize


PARAGRAPH = (
    "Commissioners voted 4-3 to approve the mesure, which raises the property "
    "tax rate by two cents per $100 of assessed value beginning in July."
)
OFFSET = PARAGRAPH.index("mesure")
LENGTH = len("mesure")


def fingerprint_for(paragraph, offset, length, rule_id="MORFOLOGIK_RULE_EN_US"):
    before, after = context_window(paragraph, offset, length)
    return issue_fingerprint(
        kind="spelling",
        rule_id=rule_id,
        matched_text=paragraph[offset : offset + length],
        context_before=before,
        context_after=after,
    )


# --- stability ------------------------------------------------------------


def fingerprint_from_body(paragraphs, needle, rule_id="MORFOLOGIK_RULE_EN_US"):
    """Go the whole way an issue actually travels: body offset -> fingerprint.

    This mirrors scan.py: LanguageTool reports an offset into the joined body,
    which is mapped back to paragraph coordinates before hashing.
    """
    body = PARAGRAPH_SEPARATOR.join(paragraphs)
    global_offset = body.index(needle)
    located = locate_offset(paragraphs, global_offset)
    assert located is not None
    para_index, local_offset = located
    return fingerprint_for(paragraphs[para_index], local_offset, len(needle), rule_id)


def test_inserting_a_paragraph_above_does_not_change_the_fingerprint():
    """The core requirement: offsets shift, identity does not."""
    lead = "County commissioners met Tuesday evening for a session that ran late."
    before_insert = [lead, PARAGRAPH]
    after_insert = [
        lead,
        "A crowd of about sixty residents filled the chamber and the overflow room.",
        PARAGRAPH,
    ]

    # The absolute offset really does move -- otherwise this test proves nothing.
    assert PARAGRAPH_SEPARATOR.join(before_insert).index("mesure") != PARAGRAPH_SEPARATOR.join(
        after_insert
    ).index("mesure")

    assert fingerprint_from_body(before_insert, "mesure") == fingerprint_from_body(
        after_insert, "mesure"
    )


def test_locate_offset_maps_body_offsets_to_paragraph_coordinates():
    paragraphs = ["First one here.", "Second one here.", "Third one here."]
    body = PARAGRAPH_SEPARATOR.join(paragraphs)
    assert locate_offset(paragraphs, body.index("First")) == (0, 0)
    assert locate_offset(paragraphs, body.index("Second")) == (1, 0)
    assert locate_offset(paragraphs, body.index("Third one")) == (2, 0)
    assert locate_offset(paragraphs, body.index("one here.", 20)) == (1, 7)


def test_locate_offset_rejects_separator_and_out_of_range():
    paragraphs = ["First one here.", "Second one here."]
    assert locate_offset(paragraphs, len("First one here.")) is None  # in the separator
    assert locate_offset(paragraphs, 10_000) is None


def test_editing_elsewhere_in_the_same_paragraph_beyond_the_window_is_stable():
    far_edit = PARAGRAPH.replace("beginning in July.", "beginning in August of next year.")
    assert fingerprint_for(far_edit, OFFSET, LENGTH) == fingerprint_for(PARAGRAPH, OFFSET, LENGTH)


def test_typography_changes_do_not_change_the_fingerprint():
    curly = PARAGRAPH.replace("4-3", "4–3")
    offset = curly.index("mesure")
    assert fingerprint_for(curly, offset, LENGTH) == fingerprint_for(PARAGRAPH, OFFSET, LENGTH)


def test_fingerprint_is_deterministic_across_calls():
    assert fingerprint_for(PARAGRAPH, OFFSET, LENGTH) == fingerprint_for(PARAGRAPH, OFFSET, LENGTH)


# --- discrimination -------------------------------------------------------


def test_correcting_the_error_changes_the_fingerprint():
    """This is what lets a rescan mark an issue resolved."""
    fixed = PARAGRAPH.replace("mesure", "measure")
    offset = fixed.index("measure")
    assert fingerprint_for(fixed, offset, len("measure")) != fingerprint_for(
        PARAGRAPH, OFFSET, LENGTH
    )


def test_same_typo_in_different_context_is_a_different_issue():
    other = "The mesure was debated for hours before the board reached a decision at last."
    offset = other.index("mesure")
    assert fingerprint_for(other, offset, LENGTH) != fingerprint_for(PARAGRAPH, OFFSET, LENGTH)


def test_different_rule_is_a_different_issue():
    assert fingerprint_for(PARAGRAPH, OFFSET, LENGTH, rule_id="OTHER_RULE") != fingerprint_for(
        PARAGRAPH, OFFSET, LENGTH
    )


# --- context window -------------------------------------------------------


def test_context_window_is_clamped_to_the_paragraph():
    before, after = context_window("Short text here.", offset=6, length=4)
    assert before == "Short "
    assert after == " here."


def test_context_window_at_paragraph_start_has_empty_before():
    before, after = context_window(PARAGRAPH, 0, 13)
    assert before == ""
    assert after


# --- duplicates -----------------------------------------------------------


def test_duplicate_fingerprint_is_order_independent():
    a, b = "The first paragraph text.", "The second paragraph text."
    assert duplicate_fingerprint("duplicate", a, b) == duplicate_fingerprint("duplicate", b, a)


def test_duplicate_fingerprint_ignores_position():
    text = "A repeated paragraph that appears twice in the article body."
    assert duplicate_fingerprint("duplicate", text) == duplicate_fingerprint("duplicate", text)


# --- normalizer -----------------------------------------------------------


def test_normalize_folds_case_punctuation_and_whitespace():
    assert normalize("  The  Board’s   decision! ") == "the boards decision"
