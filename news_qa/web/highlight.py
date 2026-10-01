"""Render an article version with its issues marked in place.

``issues.char_offset`` is paragraph-local (grammar.py stores the local offset
returned by ``fingerprint.locate_offset``), so a span is a direct splice into
``body_json[para_index].text`` -- no coordinate translation.

Duplicate issues have no span to mark: for ``kind == 'duplicate'``,
``matched_text`` is paragraph A and ``context_after`` is paragraph B. They are
listed separately rather than highlighted, the same special case report.py makes.
"""

import html
from typing import Any, Dict, Iterable, List

# Suppressed and dismissed issues are dimmed rather than hidden -- the project
# treats a suppression as a labelled judgement, not a deletion.
def _classes(issue: Dict[str, Any]) -> str:
    parts = ["mark", f"mark-{issue.get('severity') or 'error'}"]
    if issue.get("status") and issue["status"] != "open":
        parts.append(f"mark-{issue['status']}")
    if issue.get("suppressed_reason"):
        parts.append("mark-hidden")
    return " ".join(parts)


def is_duplicate(issue: Dict[str, Any]) -> bool:
    return issue.get("kind") == "duplicate"


def _spans(text: str, issues: List[Dict[str, Any]], focus_id=None) -> str:
    """Splice <mark> tags into escaped text.

    Overlapping spans are dropped rather than nested: LanguageTool occasionally
    reports two rules over the same words, and nesting them would produce
    tangled markup for no extra information.
    """
    placed = sorted(
        (
            issue
            for issue in issues
            if issue.get("char_offset") is not None and issue.get("match_length")
        ),
        key=lambda issue: (issue["char_offset"], -issue["match_length"]),
    )

    out: List[str] = []
    cursor = 0
    for issue in placed:
        start = issue["char_offset"]
        end = start + issue["match_length"]
        if start < cursor or end > len(text):
            continue
        classes = _classes(issue)
        if focus_id is not None and issue["id"] == focus_id:
            classes += " mark-focus"
        out.append(html.escape(text[cursor:start]))
        out.append(
            f'<mark id="issue-{issue["id"]}" class="{classes}"'
            f' title="{html.escape(issue.get("message") or "")}">'
            f"{html.escape(text[start:end])}</mark>"
        )
        cursor = end

    out.append(html.escape(text[cursor:]))
    return "".join(out)


def render_paragraphs(
    body: Iterable[Dict[str, Any]],
    issues: Iterable[Dict[str, Any]],
    focus_id=None,
) -> List[Dict[str, Any]]:
    """[{index, html, issues}] for a version body and the issues found in it."""
    by_para: Dict[int, List[Dict[str, Any]]] = {}
    for issue in issues:
        if is_duplicate(issue):
            continue
        index = issue.get("para_index")
        if index is None:
            continue
        by_para.setdefault(index, []).append(issue)

    rendered = []
    for paragraph in body:
        index = paragraph["index"]
        here = by_para.get(index, [])
        rendered.append(
            {
                "index": index,
                "html": _spans(paragraph["text"], here, focus_id),
                "issues": here,
            }
        )
    return rendered


def duplicate_pair(issue: Dict[str, Any]) -> List[str]:
    """The two paragraphs a duplicate issue is complaining about."""
    return [part for part in (issue.get("matched_text"), issue.get("context_after")) if part]


def excerpt(issue: Dict[str, Any]) -> Dict[str, str]:
    """The one-line context shown in list views.

    No padding around the mark: the surrounding text is reproduced exactly so
    spacing errors in the article stay visible instead of being masked by the
    UI's own formatting.
    """
    return {
        "before": issue.get("context_before") or "",
        "match": issue.get("matched_text") or "",
        "after": issue.get("context_after") or "",
    }
