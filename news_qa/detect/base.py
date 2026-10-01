"""The Issue record shared by every detector and filter."""

from dataclasses import dataclass, field
from typing import List, Optional

# kind
SPELLING = "spelling"
GRAMMAR = "grammar"
STYLE = "style"
DUPLICATE = "duplicate"

# severity
ERROR = "error"
STYLE_SEVERITY = "style"


@dataclass
class Issue:
    """A candidate finding, before it is persisted.

    Offsets are *paragraph-local*: ``char_offset`` indexes into
    ``paragraphs[para_index].text``. They exist for display and highlighting
    only -- identity comes from ``fingerprint``.
    """

    kind: str
    message: str
    matched_text: str
    severity: str = ERROR
    rule_id: Optional[str] = None
    category: Optional[str] = None
    context_before: str = ""
    context_after: str = ""
    suggestions: List[str] = field(default_factory=list)
    para_index: Optional[int] = None
    char_offset: Optional[int] = None
    match_length: Optional[int] = None
    in_quote: bool = False
    fingerprint: str = ""
    # Set by the filter chain. Non-null means "hide by default", not "discard":
    # keeping the row lets the UI explain why something was hidden and lets you
    # audit whether the filters are too aggressive.
    suppressed_reason: Optional[str] = None

    @property
    def suppressed(self) -> bool:
        return self.suppressed_reason is not None

    @property
    def is_spelling(self) -> bool:
        return self.kind == SPELLING
