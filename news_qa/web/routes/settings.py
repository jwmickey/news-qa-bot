"""The two knobs that change what future scans report: dictionary and rules."""

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import actions, queries
from ..deps import get_conn
from ..templating import templates

router = APIRouter()


@router.get("/dictionary", response_class=HTMLResponse)
def dictionary(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    q: Optional[str] = None,
):
    return templates.TemplateResponse(
        request,
        "dictionary.html",
        {"terms": queries.dictionary_terms(conn, q), "q": q or "", "nav": "dictionary"},
    )


@router.post("/dictionary/add")
def dictionary_add(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    term: str = Form(...),
):
    # A textarea or a space-separated paste both work; one term per line.
    for line in term.replace(",", "\n").splitlines():
        if line.strip():
            actions.add_term(conn, line.strip())
    conn.commit()
    return RedirectResponse("/dictionary", status_code=303)


@router.post("/dictionary/remove")
def dictionary_remove(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    term: str = Form(...),
):
    actions.remove_term(conn, term)
    conn.commit()
    return RedirectResponse("/dictionary", status_code=303)


@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    stats = queries.stats(conn)
    return templates.TemplateResponse(
        request,
        "rules.html",
        {
            "suppressed": queries.suppressed_rules(conn),
            "top_rules": stats["top_rules"],
            "hidden": stats["hidden"],
            "nav": "rules",
        },
    )


@router.post("/rules/add")
def rules_add(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    rule_id: str = Form(...),
    note: Optional[str] = Form(None),
):
    actions.suppress_rule(conn, rule_id, note)
    conn.commit()
    return RedirectResponse("/rules", status_code=303)


@router.post("/rules/remove")
def rules_remove(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    rule_id: str = Form(...),
):
    actions.unsuppress_rule(conn, rule_id)
    conn.commit()
    return RedirectResponse("/rules", status_code=303)
