"""Suppression filters -- the layer that makes the output worth reading.

Most of what LanguageTool calls a "possible spelling mistake" in local news is
a name: a source, a town, an agency, a high school. Two independent layers
handle that, because neither is sufficient alone:

* spaCy NER catches names it recognizes from context, including ones nobody
  will ever add to a list ("Renata Okonkwo").
* The dictionary catches what NER misses -- outlet names, local shorthand, and
  anything you dismiss in the UI. ("WRAL" is not tagged as an entity by the
  small model, which is precisely why both layers exist.)

Nothing is deleted. A filtered issue keeps its row with ``suppressed_reason``
set, so the UI can show what was hidden and why, and so over-aggressive
filtering is visible rather than invisible.
"""

import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .. import config
from ..extract import ExtractedArticle
from .base import Issue

# Only spelling findings are eligible for name-based suppression. A real
# grammar error that happens to sit inside a name span must still be reported.
_POSSESSIVE = re.compile(r"['’]s$", re.IGNORECASE)

# Straight and curly double quotes, paired.
_QUOTE_CHARS = '"“”'


def strip_possessive(text: str) -> str:
    return _POSSESSIVE.sub("", text).strip()


def quote_spans(text: str) -> List[Tuple[int, int]]:
    """Character ranges inside double quotes.

    Errors inside quoted speech usually belong to the speaker, not the paper,
    so they are flagged rather than hidden -- a natural filter in the UI.
    """
    spans = []
    open_at: Optional[int] = None
    for index, char in enumerate(text):
        if char not in _QUOTE_CHARS:
            continue
        if char == "“":
            open_at = index
        elif char == "”":
            if open_at is not None:
                spans.append((open_at, index))
                open_at = None
        else:  # straight quote toggles
            if open_at is None:
                open_at = index
            else:
                spans.append((open_at, index))
                open_at = None
    return spans


def _overlaps(start: int, end: int, spans: Iterable[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    for span_start, span_end in spans:
        if start < span_end and end > span_start:
            return span_start, span_end
    return None


class EntityIndex:
    """Named-entity spans per paragraph, computed once per article."""

    def __init__(self, nlp, labels: Set[str]):
        self.nlp = nlp
        self.labels = labels
        self._spans: Dict[int, List[Tuple[int, int, str]]] = {}

    def build(self, paragraphs: Sequence[str]) -> None:
        self._spans = {}
        if self.nlp is None:
            return
        for index, doc in enumerate(self.nlp.pipe(paragraphs)):
            self._spans[index] = [
                (entity.start_char, entity.end_char, entity.label_)
                for entity in doc.ents
                if entity.label_ in self.labels
            ]

    def label_at(self, para_index: int, start: int, end: int) -> Optional[str]:
        for span_start, span_end, label in self._spans.get(para_index, []):
            if start < span_end and end > span_start:
                return label
        return None


def load_nlp(model: str = config.SPACY_MODEL):
    """Load spaCy, or return None if it is unavailable.

    NER is a quality improvement, not a hard requirement: without it the
    dictionary still works and the scan still runs, just noisier.
    """
    try:
        import spacy
    except ImportError:
        return None
    try:
        # The parser and lemmatizer contribute nothing to NER and cost time.
        return spacy.load(model, disable=["parser", "lemmatizer"])
    except OSError:
        return None


class FilterChain:
    def __init__(
        self,
        dictionary: Optional[Set[str]] = None,
        suppressed_rules: Optional[Set[str]] = None,
        nlp=None,
        use_ner: bool = True,
    ):
        self.dictionary = {term.casefold() for term in (dictionary or set())}
        self.multiword = {term for term in self.dictionary if " " in term}
        self.suppressed_rules = suppressed_rules or set()
        if nlp is None and use_ner:
            nlp = load_nlp()
        self.nlp = nlp
        self.entities = EntityIndex(self.nlp, config.NAME_ENTITY_LABELS)

    @property
    def ner_available(self) -> bool:
        return self.nlp is not None

    # --- individual checks ------------------------------------------------

    def _dictionary_hit(self, issue: Issue) -> bool:
        candidate = strip_possessive(issue.matched_text).casefold()
        if not candidate:
            return False
        if candidate in self.dictionary:
            return True

        # A multi-word entry such as "chapel hill" should also cover a match on
        # just "Chapel". Require the match to be a word of the phrase and the
        # phrase to actually appear around the match, so "Person" (the county)
        # does not silently whitelist the common noun.
        neighborhood = (
            f"{issue.context_before[-40:]}{issue.matched_text}{issue.context_after[:40]}"
        ).casefold()
        for term in self.multiword:
            if candidate in term.split() and term in neighborhood:
                return True
        return False

    def _entity_label(self, issue: Issue) -> Optional[str]:
        if not self.ner_available or issue.para_index is None or issue.char_offset is None:
            return None
        start = issue.char_offset
        end = start + (issue.match_length or len(issue.matched_text))
        return self.entities.label_at(issue.para_index, start, end)

    # --- the chain --------------------------------------------------------

    def apply(self, article: ExtractedArticle, issues: List[Issue]) -> List[Issue]:
        """Annotate issues in place with suppression reasons and quote flags."""
        paragraphs = [paragraph.text for paragraph in article.paragraphs]
        if any(issue.is_spelling for issue in issues):
            self.entities.build(paragraphs)

        quotes = {
            index: quote_spans(text)
            for index, text in enumerate(paragraphs)
            if _has_quote(text)
        }

        for issue in issues:
            if issue.para_index is not None and issue.char_offset is not None:
                start = issue.char_offset
                end = start + (issue.match_length or len(issue.matched_text))
                issue.in_quote = (
                    _overlaps(start, end, quotes.get(issue.para_index, [])) is not None
                )

            if issue.rule_id and issue.rule_id in self.suppressed_rules:
                issue.suppressed_reason = "rule"
                continue

            # Name suppression applies to spelling findings only.
            if not issue.is_spelling:
                continue

            if self._dictionary_hit(issue):
                issue.suppressed_reason = "dictionary"
                continue

            label = self._entity_label(issue)
            if label:
                issue.suppressed_reason = f"entity:{label}"

        return issues


def _has_quote(text: str) -> bool:
    return any(char in text for char in _QUOTE_CHARS)
