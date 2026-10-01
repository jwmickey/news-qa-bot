"""Detectors and suppression filters.

``run_detectors`` is the single entry point the scanner uses. Adding a detector
-- AP style rules, headline checks, or the on-demand LLM reviewer sketched for
a later phase -- means appending to the list here; nothing else changes.
"""

from typing import List, Optional

from ..extract import ExtractedArticle
from .base import DUPLICATE, ERROR, GRAMMAR, SPELLING, STYLE, Issue
from .duplicates import find_duplicates
from .filters import FilterChain
from .grammar import GrammarChecker

__all__ = [
    "DUPLICATE",
    "ERROR",
    "GRAMMAR",
    "SPELLING",
    "STYLE",
    "FilterChain",
    "GrammarChecker",
    "Issue",
    "find_duplicates",
    "run_detectors",
]


def run_detectors(
    article: ExtractedArticle,
    checker: GrammarChecker,
    filters: Optional[FilterChain] = None,
) -> List[Issue]:
    """Detect, then filter. Suppressed issues are annotated, not dropped."""
    issues: List[Issue] = []
    issues.extend(checker.check(article))
    issues.extend(find_duplicates(article))

    if filters is not None:
        filters.apply(article, issues)

    return issues
