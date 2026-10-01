"""Shared text normalization.

Used both to compare paragraphs for duplicate detection and to build issue
fingerprints, so the two agree on what "the same text" means.
"""

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

# Curly quotes and dashes vary between a CMS's rendering of the same sentence;
# folding them keeps a fingerprint stable when only the typography changes.
_TYPOGRAPHY = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "…": "...",
    }
)


def normalize(text: str) -> str:
    """Casefold, fold typography, drop punctuation, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHY)
    text = _PUNCT.sub("", text.casefold())
    return _WHITESPACE.sub(" ", text).strip()


def word_count(text: str) -> int:
    return len(normalize(text).split())
