"""Dashboard and per-source views.

The dashboard is a grid of source cards rather than a WRAL-shaped page. With
one source it reads as a detail view; with six it still reads.
"""

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import highlight, queries
from ..deps import get_conn
from ..templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    # The page opens on the most recently flagged error, in its own words.
    # A count tells you there is work; the sentence tells you what the work is.
    newest, _ = queries.list_issues(conn, status="open", severity="error", limit=1)
    hero = newest[0] if newest else None
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "hero": hero,
            "hero_excerpt": highlight.excerpt(hero) if hero else None,
            "hero_is_duplicate": highlight.is_duplicate(hero) if hero else False,
            "hero_pair": highlight.duplicate_pair(hero) if hero else [],
            "sources": queries.list_sources(conn),
            "jobs": queries.active_jobs(conn),
            "recent_jobs": queries.recent_jobs(conn, limit=5),
            "scans": queries.list_scans(conn, limit=8),
            "stats": queries.stats(conn),
            "nav": "dashboard",
        },
    )


@router.get("/sources", response_class=HTMLResponse)
def source_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request,
        "sources.html",
        {"sources": queries.list_sources(conn), "nav": "sources"},
    )


@router.get("/sources/{key}", response_class=HTMLResponse)
def source_detail(
    request: Request,
    key: str,
    conn: sqlite3.Connection = Depends(get_conn),
    status: Optional[str] = "open",
):
    source = queries.get_source(conn, key)
    if source is None:
        return templates.TemplateResponse(
            request, "not_found.html", {"what": f"source {key!r}"}, status_code=404
        )
    issues, total = queries.list_issues(conn, source=key, status=status, limit=25)
    return templates.TemplateResponse(
        request,
        "source_detail.html",
        {
            "source": source,
            "stats": queries.stats(conn, source=key),
            "scans": queries.list_scans(conn, source=key, limit=10),
            "jobs": queries.active_jobs(conn),
            "issues": issues,
            "issue_total": total,
            "status": status,
            "nav": "sources",
        },
    )


@router.get("/health")
def health(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    queue = request.app.state.queue
    return {
        "ok": True,
        "worker_alive": queue.alive,
        "current_job": queue.current_job_id,
        "queued": len(queries.active_jobs(conn)),
    }
