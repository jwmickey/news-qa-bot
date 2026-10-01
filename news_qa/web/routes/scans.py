"""Scan history and the trigger/monitor endpoints."""

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import queries
from ..deps import get_conn
from ..jobs import JobParams, JobRejected
from ..templating import is_htmx, templates

router = APIRouter()


@router.get("/scans", response_class=HTMLResponse)
def scan_list(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    source: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    return templates.TemplateResponse(
        request,
        "scans.html",
        {
            "scans": queries.list_scans(conn, source=source, limit=limit, offset=offset),
            "sources": queries.list_sources(conn),
            "filters": {"source": source, "limit": limit, "offset": offset},
            "nav": "scans",
        },
    )


@router.get("/scans/{scan_id}", response_class=HTMLResponse)
def scan_detail(
    request: Request, scan_id: int, conn: sqlite3.Connection = Depends(get_conn)
):
    scan = queries.get_scan(conn, scan_id)
    if scan is None:
        return templates.TemplateResponse(
            request, "not_found.html", {"what": f"scan #{scan_id}"}, status_code=404
        )
    issues, total = queries.list_issues(
        conn, scan_id=scan_id, status=None, include_hidden=True, limit=500
    )
    return templates.TemplateResponse(
        request,
        "scan_detail.html",
        {
            "scan": scan,
            "articles": queries.scan_articles(conn, scan_id),
            "grouped": queries.group_by_article([i for i in issues if i["status"] == "open"]),
            "issue_total": total,
            "nav": "scans",
        },
    )


# --- triggers --------------------------------------------------------------


@router.post("/sources/{key}/{kind}", response_class=HTMLResponse)
def trigger(
    request: Request,
    key: str,
    kind: str,
    conn: sqlite3.Connection = Depends(get_conn),
    limit: Optional[str] = Form(None),
    since: Optional[str] = Form(None),
    all_articles: Optional[str] = Form(None),
    no_ner: Optional[str] = Form(None),
):
    if kind not in ("scan", "rescan"):
        return templates.TemplateResponse(
            request, "not_found.html", {"what": f"action {kind!r}"}, status_code=404
        )

    params = JobParams(
        limit=int(limit) if limit else None,
        since_days=int(since) if since else None,
        open_only=not all_articles,
        use_ner=not no_ner,
    )

    error = None
    try:
        request.app.state.queue.enqueue(conn, key, kind=kind, params=params)
    except JobRejected as rejected:
        error = str(rejected)
    except KeyError:
        error = f"Unknown source {key!r}."

    if is_htmx(request):
        return _jobs_panel(request, conn, error=error)
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@router.get("/jobs/active", response_class=HTMLResponse)
def jobs_panel(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Polled by HTMX. Cheap enough to hit every couple of seconds."""
    return _jobs_panel(request, conn)


def _jobs_panel(request: Request, conn: sqlite3.Connection, error: Optional[str] = None):
    jobs = queries.active_jobs(conn)
    response = templates.TemplateResponse(
        request,
        "partials/jobs.html",
        {"jobs": jobs, "recent_jobs": queries.recent_jobs(conn, limit=5), "error": error},
    )
    # When the queue drains, tell the page to refresh its counts once rather
    # than polling stale numbers forever.
    if not jobs:
        response.headers["HX-Trigger"] = "queue-idle"
    return response
