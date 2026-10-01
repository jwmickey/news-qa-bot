"""Repeated-paragraph detection.

Ported from the original ``find_duplicate_paragraphs``. The algorithm is
unchanged in substance -- exact matches by normalized text, then fuzzy matches
by difflib ratio -- but it now works on the structured paragraph list and emits
Issues. Its old false positives came from scraping page furniture, which
extract.py no longer does, not from the comparison itself.

The all-pairs loop is quadratic, which is fine at article scale (60 paragraphs
is 1,770 comparisons). A length-ratio precheck skips the expensive
SequenceMatcher call for pairs that cannot possibly clear the threshold.
"""

import difflib
from collections import defaultdict
from typing import List, Sequence

from .. import config
from ..extract import ExtractedArticle
from ..fingerprint import duplicate_fingerprint
from ..textnorm import normalize
from .base import DUPLICATE, ERROR, Issue

# Long enough that two near-duplicate paragraphs visibly diverge in the report.
# Near-duplicates typically share an opening, so a short preview shows two
# identical-looking strings and tells you nothing.
PREVIEW_CHARS = 320


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def find_duplicates(
    article: ExtractedArticle,
    min_words: int = config.DUPLICATE_MIN_WORDS,
    fuzzy_min_words: int = config.DUPLICATE_FUZZY_MIN_WORDS,
    fuzzy_threshold: float = config.DUPLICATE_FUZZY_THRESHOLD,
) -> List[Issue]:
    paragraphs = [paragraph.text for paragraph in article.paragraphs]
    return _find(paragraphs, min_words, fuzzy_min_words, fuzzy_threshold)


def _find(
    paragraphs: Sequence[str],
    min_words: int,
    fuzzy_min_words: int,
    fuzzy_threshold: float,
) -> List[Issue]:
    candidates = []
    for index, text in enumerate(paragraphs):
        normalized = normalize(text)
        if len(normalized.split()) < min_words:
            continue
        candidates.append((index, text, normalized))

    issues: List[Issue] = []

    # --- exact repeats ----------------------------------------------------
    by_text = defaultdict(list)
    for index, text, normalized in candidates:
        by_text[normalized].append((index, text))

    exact_group_of = {}
    for group_id, occurrences in enumerate(
        group for group in by_text.values() if len(group) > 1
    ):
        positions = [position for position, _ in occurrences]
        text = occurrences[0][1]
        for position in positions:
            exact_group_of[position] = group_id

        issues.append(
            Issue(
                kind=DUPLICATE,
                severity=ERROR,
                rule_id="DUPLICATE_PARAGRAPH",
                category="DUPLICATION",
                message=(
                    f"This paragraph appears {len(positions)} times "
                    f"(paragraphs {', '.join(str(p) for p in positions)})."
                ),
                matched_text=_preview(text),
                para_index=positions[0],
                fingerprint=duplicate_fingerprint("duplicate-exact", text),
            )
        )

    # --- near-duplicates --------------------------------------------------
    # Only substantial paragraphs are eligible; see DUPLICATE_FUZZY_MIN_WORDS.
    fuzzy_candidates = [
        candidate
        for candidate in candidates
        if len(candidate[2].split()) >= fuzzy_min_words
    ]

    for left in range(len(fuzzy_candidates)):
        index_one, text_one, normalized_one = fuzzy_candidates[left]
        for right in range(left + 1, len(fuzzy_candidates)):
            index_two, text_two, normalized_two = fuzzy_candidates[right]

            # Already reported as an exact repeat of each other.
            if (
                index_one in exact_group_of
                and index_two in exact_group_of
                and exact_group_of[index_one] == exact_group_of[index_two]
            ):
                continue

            # Two strings whose lengths differ by more than the threshold
            # allows can never reach it; skip the O(n*m) comparison.
            shorter, longer = sorted((len(normalized_one), len(normalized_two)))
            if longer == 0 or (2.0 * shorter) / (shorter + longer) < fuzzy_threshold:
                continue

            ratio = difflib.SequenceMatcher(None, normalized_one, normalized_two).ratio()
            if ratio < fuzzy_threshold:
                continue

            issues.append(
                Issue(
                    kind=DUPLICATE,
                    severity=ERROR,
                    rule_id="NEAR_DUPLICATE_PARAGRAPH",
                    category="DUPLICATION",
                    message=(
                        f"Paragraphs {index_one} and {index_two} are "
                        f"{int(ratio * 100)}% similar -- please verify."
                    ),
                    matched_text=_preview(text_one),
                    context_after=_preview(text_two),
                    para_index=index_one,
                    fingerprint=duplicate_fingerprint(
                        "duplicate-fuzzy", text_one, text_two
                    ),
                )
            )

    return issues
