"""Filter-chain tests.

Covers the two suppression layers and, just as importantly, what they must
*not* suppress.
"""

import pytest

from news_qa.detect.base import GRAMMAR, SPELLING, Issue
from news_qa.detect.filters import FilterChain, quote_spans, strip_possessive
from news_qa.detect.grammar import classify
from news_qa.extract import ExtractedArticle, Paragraph


def make_article(*texts):
    return ExtractedArticle(
        url="https://example.com/a",
        paragraphs=[Paragraph(index=i, text=t) for i, t in enumerate(texts)],
    )


def make_issue(paragraph, needle, kind=SPELLING, rule_id="MORFOLOGIK_RULE_EN_US"):
    offset = paragraph.index(needle)
    return Issue(
        kind=kind,
        rule_id=rule_id,
        message="Possible spelling mistake found.",
        matched_text=needle,
        context_before=paragraph[max(0, offset - 60) : offset],
        context_after=paragraph[offset + len(needle) : offset + len(needle) + 60],
        para_index=0,
        char_offset=offset,
        match_length=len(needle),
    )


# --- dictionary layer -----------------------------------------------------


def test_dictionary_suppresses_a_known_term():
    text = "The WRAL team reported live from the scene throughout the evening."
    article = make_article(text)
    issue = make_issue(text, "WRAL")
    FilterChain(dictionary={"wral"}, use_ner=False).apply(article, [issue])
    assert issue.suppressed_reason == "dictionary"


def test_dictionary_matching_is_case_insensitive_and_handles_possessives():
    text = "Fuquay-Varina's board met Tuesday to discuss the proposed annexation."
    article = make_article(text)
    issue = make_issue(text, "Fuquay-Varina's")
    FilterChain(dictionary={"fuquay-varina"}, use_ner=False).apply(article, [issue])
    assert issue.suppressed_reason == "dictionary"


def test_multiword_entry_covers_a_single_word_match_in_context():
    text = "Officials in Chapel Hill said the project would begin in the fall."
    article = make_article(text)
    issue = make_issue(text, "Chapel")
    FilterChain(dictionary={"chapel hill"}, use_ner=False).apply(article, [issue])
    assert issue.suppressed_reason == "dictionary"


def test_multiword_entry_does_not_whitelist_the_word_elsewhere():
    """'Person County' must not make the common noun 'person' unflaggable."""
    text = "Every person who attended the meeting was asked to sign in at the door."
    article = make_article(text)
    issue = make_issue(text, "person")
    FilterChain(dictionary={"person county"}, use_ner=False).apply(article, [issue])
    assert issue.suppressed_reason is None


def test_dictionary_does_not_suppress_an_unrelated_misspelling():
    text = "The mesure passed after a long debate among the assembled commissioners."
    article = make_article(text)
    issue = make_issue(text, "mesure")
    FilterChain(dictionary={"wral", "raleigh"}, use_ner=False).apply(article, [issue])
    assert issue.suppressed_reason is None


# --- NER layer ------------------------------------------------------------


@pytest.fixture(scope="module")
def nlp():
    from news_qa.detect.filters import load_nlp

    model = load_nlp()
    if model is None:
        pytest.skip("spaCy model en_core_web_sm is not installed")
    return model


def test_ner_suppresses_a_person_name_not_in_the_dictionary(nlp):
    text = "Renata Okonkwo told commissioners that the county had no realistic alternative."
    article = make_article(text)
    issue = make_issue(text, "Okonkwo")
    FilterChain(dictionary=set(), nlp=nlp).apply(article, [issue])
    assert issue.suppressed_reason is not None
    assert issue.suppressed_reason.startswith("entity:")


def test_ner_does_not_suppress_a_real_misspelling(nlp):
    text = "Mostly cloudy sies with a few showers and storms still possible on Tuesday."
    article = make_article(text)
    issue = make_issue(text, "sies")
    FilterChain(dictionary=set(), nlp=nlp).apply(article, [issue])
    assert issue.suppressed_reason is None


def test_ner_never_suppresses_a_grammar_error_inside_a_name(nlp):
    """A repeated word next to a name is still a repeated word."""
    text = "Renata Okonkwo said the the vote was closer than anyone had expected."
    article = make_article(text)
    issue = make_issue(text, "the the", kind=GRAMMAR, rule_id="ENGLISH_WORD_REPEAT_RULE")
    FilterChain(dictionary=set(), nlp=nlp).apply(article, [issue])
    assert issue.suppressed_reason is None


# --- rule suppression -----------------------------------------------------


def test_suppressed_rule_is_filtered_regardless_of_kind():
    text = "The board’s decision came after three hours of debate in the chamber."
    article = make_article(text)
    issue = make_issue(text, "board’s", kind=GRAMMAR, rule_id="EN_QUOTES")
    FilterChain(suppressed_rules={"EN_QUOTES"}, use_ner=False).apply(article, [issue])
    assert issue.suppressed_reason == "rule"


# --- quote tagging --------------------------------------------------------


def test_errors_inside_quoted_speech_are_tagged_not_hidden():
    text = 'The mayor said, "We was ready for this," before taking questions from reporters.'
    article = make_article(text)
    issue = make_issue(text, "We was", kind=GRAMMAR, rule_id="PERS_PRONOUN_AGREEMENT")
    FilterChain(use_ner=False).apply(article, [issue])
    assert issue.in_quote is True
    assert issue.suppressed_reason is None


def test_errors_outside_quotes_are_not_tagged():
    text = 'The mayor said, "We were ready," but the councl disagreed with that account.'
    article = make_article(text)
    issue = make_issue(text, "councl")
    FilterChain(use_ner=False).apply(article, [issue])
    assert issue.in_quote is False


def test_quote_spans_handles_curly_and_straight_quotes():
    assert quote_spans('say "hi" now') == [(4, 7)]
    assert quote_spans("say “hi” now") == [(4, 7)]
    assert quote_spans("no quotes here") == []


def test_strip_possessive():
    assert strip_possessive("Okonkwo's") == "Okonkwo"
    assert strip_possessive("Okonkwo’s") == "Okonkwo"
    assert strip_possessive("Okonkwo") == "Okonkwo"


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    "category,issue_type,expected_kind,expected_severity",
    [
        ("TYPOS", "misspelling", "spelling", "error"),
        ("MISC", "duplication", "grammar", "error"),
        ("GRAMMAR", "grammar", "grammar", "error"),
        ("REDUNDANCY", "style", "style", "style"),
        ("STYLE", "style", "style", "style"),
    ],
)
def test_classify(category, issue_type, expected_kind, expected_severity):
    kind, severity = classify(category, issue_type)
    assert (kind, severity) == (expected_kind, expected_severity)


# --- filters degrade gracefully -------------------------------------------


def test_chain_runs_without_spacy():
    text = "The mesure passed after a long debate among the assembled commissioners."
    article = make_article(text)
    issue = make_issue(text, "mesure")
    chain = FilterChain(dictionary=set(), nlp=None, use_ner=False)
    assert chain.ner_available is False
    chain.apply(article, [issue])  # must not raise
    assert issue.suppressed_reason is None
