"""Issue triage: list, review in context, dismiss, reopen."""

import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import actions, highlight, queries
from ..deps import get_conn
from ..templating import is_htmx, templates

router = APIRouter()

PAGE_SIZE = 50


def _blank(value: Optional[str]) -> Optional[str]:
    """Treat 'all' and empty selects as no filter."""
    return None if value in (None, "", "all") else value


@router.get("/issues", response_class=HTMLResponse)
def issue_list(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    source: Optional[str] = None,
    status: Optional[str] = "open",
    severity: Optional[str] = None,
    kind: Optional[str] = None,
    rule_id: Optional[str] = None,
    q: Optional[str] = None,
    include_hidden: bool = False,
    offset: int = 0,
):
    filters = {
        "source": _blank(source),
        "status": _blank(status),
        "severity": _blank(severity),
        "kind": _blank(kind),
        "rule_id": _blank(rule_id),
        "q": q or None,
        "include_hidden": include_hidden,
    }
    found, total = queries.list_issues(
        conn,
        query=filters["q"],
        limit=PAGE_SIZE,
        offset=offset,
        **{key: filters[key] for key in ("source", "status", "severity", "kind", "rule_id")},
        include_hidden=include_hidden,
    )
    return templates.TemplateResponse(
        request,
        "issues.html",
        {
            "grouped": queries.group_by_article(found),
            "count": len(found),
            "total": total,
            "offset": offset,
            "page_size": PAGE_SIZE,
            "filters": {**filters, "offset": offset},
            "sources": queries.list_sources(conn),
            "statuses": queries.ISSUE_STATUSES,
            "severities": queries.SEVERITIES,
            "kinds": queries.KINDS,
            "nav": "issues",
        },
    )


@router.get("/issues/{issue_id}", response_class=HTMLResponse)
def issue_detail(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    issue = queries.get_issue(conn, issue_id)
    if issue is None:
        return templates.TemplateResponse(
            request, "not_found.html", {"what": f"issue #{issue_id}"}, status_code=404
        )

    # Render the version the issue was last seen in, not merely the newest one:
    # for a resolved issue the newest version no longer contains the span.
    version_id = issue["last_seen_version_id"] or queries.latest_version_id(
        conn, issue["article_id"]
    )
    siblings, _ = queries.list_issues(
        conn, article_id=issue["article_id"], status=None, include_hidden=True, limit=500
    )
    body = queries.version_body(conn, version_id) if version_id else []

    return templates.TemplateResponse(
        request,
        "issue_detail.html",
        {
            "issue": issue,
            "paragraphs": highlight.render_paragraphs(body, siblings, focus_id=issue_id),
            "siblings": [s for s in siblings if s["id"] != issue_id],
            "dismissals": queries.issue_history(conn, issue_id),
            # Prefixed so they cannot shadow the same-named global helpers the
            # shared macros call.
            "this_suggestions": queries.suggestions(issue),
            "this_pair": highlight.duplicate_pair(issue),
            "this_is_duplicate": highlight.is_duplicate(issue),
            "this_excerpt": highlight.excerpt(issue),
            "version_id": version_id,
            "nav": "issues",
        },
    )


# --- write actions ---------------------------------------------------------


def _dictionary_flash(result: dict) -> str:
    """Say what the dictionary did — "unchanged" alone reads as a failure."""
    term = result["term"]
    hidden = result["also_hidden"]
    also = (
        f" Hid {hidden} other open {'issue' if hidden == 1 else 'issues'} matching it."
        if hidden
        else ""
    )
    return {
        "added": f" Added “{term}” to the dictionary.{also}",
        "already-present": f" “{term}” was already in the dictionary.",
        "not-spelling": " Only spelling matches go in the dictionary; not added.",
        "no-term": " No term to add to the dictionary.",
    }.get(result["dictionary"], "")


def _row_response(request: Request, conn: sqlite3.Connection, issue_id: int, flash=None):
    """Swap just the affected row so a long triage list keeps its scroll."""
    return templates.TemplateResponse(
        request,
        "partials/issue_row.html",
        {"issue": queries.get_issue(conn, issue_id), "flash": flash},
    )


@router.post("/issues/{issue_id}/dismiss", response_class=HTMLResponse)
def dismiss(
    request: Request,
    issue_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    reason: str = Form("not-an-error"),
    note: Optional[str] = Form(None),
    add_to_dictionary: Optional[str] = Form(None),
):
    result = actions.dismiss(
        conn,
        issue_id,
        reason=reason,
        note=note,
        add_to_dictionary=bool(add_to_dictionary),
    )
    conn.commit()

    flash = f"Dismissed as {reason}."
    if add_to_dictionary:
        flash += _dictionary_flash(result)
    if not is_htmx(request):
        return RedirectResponse(request.headers.get("referer") or "/issues", status_code=303)
    return _row_response(request, conn, issue_id, flash=flash)


@router.post("/issues/{issue_id}/reopen", response_class=HTMLResponse)
def reopen(
    request: Request, issue_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    actions.reopen(conn, issue_id)
    conn.commit()
    if not is_htmx(request):
        return RedirectResponse(request.headers.get("referer") or "/issues", status_code=303)
    return _row_response(request, conn, issue_id, flash="Reopened.")


@router.post("/issues/bulk", response_class=HTMLResponse)
def bulk(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    issue_ids: List[int] = Form(default=[]),
    action: str = Form("dismiss"),
    reason: str = Form("not-an-error"),
    add_to_dictionary: Optional[str] = Form(None),
):
    for issue_id in issue_ids:
        try:
            if action == "reopen":
                actions.reopen(conn, issue_id)
            else:
                actions.dismiss(
                    conn,
                    issue_id,
                    reason=reason,
                    add_to_dictionary=bool(add_to_dictionary),
                )
        except KeyError:
            continue
    conn.commit()
    return RedirectResponse(request.headers.get("referer") or "/issues", status_code=303)
