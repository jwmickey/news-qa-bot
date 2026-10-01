"""Article view and version diff.

``article_versions.body_json`` stores the exact paragraph list handed to the
detectors, which is what makes both of these possible: any past version can be
re-rendered, and two of them can be compared.
"""

import difflib
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import highlight, queries
from ..deps import get_conn
from ..templating import templates

router = APIRouter()


@router.get("/articles/{article_id}", response_class=HTMLResponse)
def article_detail(
    request: Request,
    article_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    version: Optional[int] = None,
    include_hidden: bool = True,
):
    article = queries.get_article(conn, article_id)
    if article is None:
        return templates.TemplateResponse(
            request, "not_found.html", {"what": f"article #{article_id}"}, status_code=404
        )

    versions = queries.article_versions(conn, article_id)
    version_id = version or (versions[0]["id"] if versions else None)
    issues, _ = queries.list_issues(
        conn,
        article_id=article_id,
        status=None,
        include_hidden=include_hidden,
        limit=500,
    )
    body = queries.version_body(conn, version_id) if version_id else []

    return templates.TemplateResponse(
        request,
        "article_detail.html",
        {
            "article": article,
            "versions": versions,
            "version_id": version_id,
            "paragraphs": highlight.render_paragraphs(body, issues),
            "issues": issues,
            "duplicates": [i for i in issues if highlight.is_duplicate(i)],
            "duplicate_pair": highlight.duplicate_pair,
            "open_count": sum(1 for i in issues if i["status"] == "open"),
            "nav": "issues",
        },
    )


@router.get("/articles/{article_id}/diff", response_class=HTMLResponse)
def article_diff(
    request: Request,
    article_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    a: Optional[int] = None,
    b: Optional[int] = None,
):
    article = queries.get_article(conn, article_id)
    versions = queries.article_versions(conn, article_id)
    if article is None or len(versions) < 2:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"what": f"a second version of article #{article_id} to compare"},
            status_code=404,
        )

    # Default to the two most recent, oldest on the left.
    newer = b or versions[0]["id"]
    older = a or versions[1]["id"]

    def lines(version_id: int):
        return [para["text"] for para in queries.version_body(conn, version_id)]

    diff = list(
        difflib.unified_diff(lines(older), lines(newer), lineterm="", n=1)
    )
    return templates.TemplateResponse(
        request,
        "article_diff.html",
        {
            "article": article,
            "versions": versions,
            "older": older,
            "newer": newer,
            "diff": diff[2:],  # drop the ---/+++ header, the selects say which
            "nav": "issues",
        },
    )
