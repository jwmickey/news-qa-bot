"""Shared Jinja environment, filters, and the HTMX request helper."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from fastapi import Request
from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def dt(value: Optional[str], fmt: str = "%Y-%m-%d %H:%M") -> str:
    parsed = _parse(value)
    return parsed.astimezone().strftime(fmt) if parsed else "—"


def ago(value: Optional[str]) -> str:
    """Relative time. Scan freshness is what the eye wants, not a timestamp."""
    parsed = _parse(value)
    if parsed is None:
        return "never"
    seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    if seconds < 0:
        return "just now"
    for limit, divisor, unit in (
        (90, 1, "s"),
        (5400, 60, "m"),
        (172800, 3600, "h"),
    ):
        if seconds < limit:
            return f"{int(seconds // divisor)}{unit} ago"
    return f"{int(seconds // 86400)}d ago"


def duration(start: Optional[str], end: Optional[str]) -> str:
    first, last = _parse(start), _parse(end)
    if not first or not last:
        return "—"
    seconds = int((last - first).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def domain(url: Optional[str]) -> str:
    return urlparse(url or "").netloc.removeprefix("www.")


def qs(params: dict, **overrides: Any) -> str:
    """Build a query string from the current filters plus overrides.

    Lets a template link to 'the same view, but page 2' without every link
    having to restate every filter.
    """
    merged = {**params, **overrides}
    clean = {
        key: value
        for key, value in merged.items()
        if value not in (None, "", False) and not (key == "offset" and value == 0)
    }
    return f"?{urlencode(clean)}" if clean else ""


def register_helpers() -> None:
    """Expose the display helpers the shared macros need.

    They are globals rather than per-route context because every view that
    renders an issue needs the same four, and a macro that silently loses one
    fails as an unhelpful "not callable" at render time.
    """
    from . import highlight, queries

    templates.env.globals.update(
        excerpt=highlight.excerpt,
        is_duplicate=highlight.is_duplicate,
        duplicate_pair=highlight.duplicate_pair,
        suggestions=queries.suggestions,
        reasons=queries.DISMISS_REASONS,
    )


templates.env.filters["dt"] = dt
templates.env.filters["ago"] = ago
templates.env.filters["domain"] = domain
templates.env.globals["duration"] = duration
templates.env.globals["qs"] = qs
register_helpers()
