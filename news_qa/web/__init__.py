"""Local review UI over the news_qa database.

Run it with ``python -m news_qa.web``. The web layer owns no data model of its
own: it reads and writes the same SQLite schema the CLI does, so a dismissal
made in the browser is visible to ``python -m news_qa issues`` immediately.
"""

from .app import create_app

__all__ = ["create_app"]
