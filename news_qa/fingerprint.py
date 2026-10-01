"""Stable identity for an issue across rescans.

A character offset cannot serve as an issue's identity: editing one paragraph
shifts every offset below it, so on the next scan every dismissed issue would
come back as new and nothing could ever be marked fixed. Instead an issue is
identified by its *content* -- the rule that fired, the text it matched, and a
window of surrounding text.

The window is clamped to the paragraph containing the match. That is the detail
that makes this work: if it spilled into neighbouring paragraphs, inserting a
paragraph above a match would change its fingerprint and resurrect the issue.

Fingerprints are stored under a UNIQUE(article_id, fingerprint) constraint, so
the article is not part of the hash -- which leaves the door open to spotting
the same recurring error across different articles later.

Changing CONTEXT_WINDOW, the normalizer, or this recipe invalidates every
stored fingerprint and would orphan existing dismissals.
"""

import hashlib
from typing import List, Optional, Sequence, Tuple

from .config import CONTEXT_WINDOW
from .textnorm import normalize

FINGERPRINT_VERSION = "1"

# extract.ExtractedArticle.body_text joins paragraphs with this separator.
PARAGRAPH_SEPARATOR = "\n\n"


def locate_offset(
    paragraph_texts: Sequence[str], global_offset: int
) -> Optional[Tuple[int, int]]:
    """Map an offset in the joined body back to (paragraph index, local offset).

    LanguageTool reports offsets into the single string we hand it; everything
    downstream -- fingerprints, UI highlighting -- needs paragraph coordinates.
    Returns None if the offset falls in a separator or past the end.
    """
    cursor = 0
    separator = len(PARAGRAPH_SEPARATOR)
    for index, text in enumerate(paragraph_texts):
        end = cursor + len(text)
        if cursor <= global_offset < end:
            return index, global_offset - cursor
        # An offset exactly at a paragraph's end belongs to that paragraph when
        # it is the last one; otherwise it is inside the separator.
        if global_offset == end and index == len(paragraph_texts) - 1:
            return index, global_offset - cursor
        cursor = end + separator
    return None


def context_window(
    paragraph_text: str,
    offset: int,
    length: int,
    window: int = CONTEXT_WINDOW,
) -> Tuple[str, str]:
    """Return (before, after) text around a match, clamped to the paragraph.

    ``offset`` is relative to the start of ``paragraph_text``.
    """
    start = max(0, offset)
    end = min(len(paragraph_text), offset + max(length, 0))
    before = paragraph_text[max(0, start - window) : start]
    after = paragraph_text[end : end + window]
    return before, after


def issue_fingerprint(
    kind: str,
    rule_id: Optional[str],
    matched_text: str,
    context_before: str = "",
    context_after: str = "",
) -> str:
    """Hash the content-identity of an issue."""
    parts = [
        FINGERPRINT_VERSION,
        kind or "",
        rule_id or "",
        normalize(matched_text),
        normalize(context_before),
        normalize(context_after),
    ]
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def duplicate_fingerprint(kind: str, *paragraph_texts: str) -> str:
    """Identity for a duplicate-paragraph finding.

    Keyed on the normalized paragraph text alone, so the finding survives the
    paragraphs moving around the article. Texts are sorted so a fuzzy pair
    reported in either order produces the same fingerprint.
    """
    normalized = sorted(normalize(text) for text in paragraph_texts)
    payload = "\x1f".join([FINGERPRINT_VERSION, kind, *normalized])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
