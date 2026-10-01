"""LanguageTool wrapper.

Runs a local LanguageTool server over the article body and converts its matches
into Issues with paragraph-local coordinates and stable fingerprints.
"""

from typing import List, Optional

from .. import config
from ..extract import ExtractedArticle
from ..fingerprint import context_window, issue_fingerprint, locate_offset
from .base import ERROR, GRAMMAR, SPELLING, STYLE, STYLE_SEVERITY, Issue

# LanguageTool's own taxonomy, used to sort findings into "a reader would call
# this an error" versus "this is a preference".
SPELLING_ISSUE_TYPES = {"misspelling"}
SPELLING_CATEGORIES = {"TYPOS"}
STYLE_ISSUE_TYPES = {"style", "register", "locale-violation"}
STYLE_CATEGORIES = {
    "STYLE",
    "REDUNDANCY",
    "PLAIN_ENGLISH",
    "WORDINESS",
    "COLLOQUIALISMS",
    "CREATIVE_WRITING",
}


def classify(category: Optional[str], issue_type: Optional[str]) -> tuple:
    """Return (kind, severity) for a LanguageTool match."""
    category = (category or "").upper()
    issue_type = (issue_type or "").lower()

    if issue_type in SPELLING_ISSUE_TYPES or category in SPELLING_CATEGORIES:
        return SPELLING, ERROR
    if issue_type in STYLE_ISSUE_TYPES or category in STYLE_CATEGORIES:
        return STYLE, STYLE_SEVERITY
    return GRAMMAR, ERROR


class GrammarChecker:
    """Owns the LanguageTool server for the life of a scan.

    Starting the server is expensive (it is a Java process), so one instance is
    created per scan and reused across articles.
    """

    def __init__(self, language: str = config.LANGUAGETOOL_LANG):
        self.language = language
        self._tool = None

    @property
    def tool(self):
        if self._tool is None:
            import language_tool_python

            self._tool = language_tool_python.LanguageTool(self.language)
        return self._tool

    def check(self, article: ExtractedArticle) -> List[Issue]:
        if not article.paragraphs:
            return []

        paragraph_texts = [paragraph.text for paragraph in article.paragraphs]
        issues: List[Issue] = []

        for match in self.tool.check(article.body_text):
            located = locate_offset(paragraph_texts, match.offset)
            if located is None:
                # The match landed on a paragraph separator; nothing to report.
                continue
            para_index, local_offset = located
            paragraph = paragraph_texts[para_index]

            # A match may not run past the paragraph it started in.
            length = min(match.error_length, len(paragraph) - local_offset)
            if length <= 0:
                continue

            matched_text = paragraph[local_offset : local_offset + length]
            before, after = context_window(paragraph, local_offset, length)
            kind, severity = classify(match.category, match.rule_issue_type)

            issues.append(
                Issue(
                    kind=kind,
                    severity=severity,
                    rule_id=match.rule_id,
                    category=match.category,
                    message=match.message,
                    matched_text=matched_text,
                    context_before=before,
                    context_after=after,
                    suggestions=list(match.replacements or [])[:8],
                    para_index=para_index,
                    char_offset=local_offset,
                    match_length=length,
                    fingerprint=issue_fingerprint(
                        kind=kind,
                        rule_id=match.rule_id,
                        matched_text=matched_text,
                        context_before=before,
                        context_after=after,
                    ),
                )
            )

        return issues

    def close(self) -> None:
        if self._tool is not None:
            self._tool.close()
            self._tool = None

    def __enter__(self) -> "GrammarChecker":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
