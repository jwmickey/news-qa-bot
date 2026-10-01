"""Application assembly and the ``python -m news_qa.web`` entry point."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from .. import config
from . import deps
from .jobs import ScanQueue, reap_orphans
from .routes import ROUTERS
from .templating import STATIC_DIR, templates


def create_app(db_path: Optional[Path] = None, start_worker: bool = True) -> FastAPI:
    deps.set_db_path(db_path)
    queue = ScanQueue(db_path=db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The one place the schema is applied; every other connection opens
        # without migrating so it never takes the write lock to do nothing.
        deps.migrate_once()

        # A crash leaves rows reading 'running' forever, which would also make
        # the duplicate-job guard refuse every future scan of that source.
        conn = deps.connect()
        try:
            reap_orphans(conn)
        finally:
            conn.close()
        if start_worker:
            queue.start()
        yield
        queue.stop()

    app = FastAPI(title="News QA", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.queue = queue
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for router in ROUTERS:
        app.include_router(router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # noqa: ANN001
        return templates.TemplateResponse(
            request, "not_found.html", {"what": request.url.path}, status_code=404
        )

    return app


def main() -> int:
    import uvicorn

    config.ensure_data_dir()
    print(f"News QA UI  →  http://{config.WEB_HOST}:{config.WEB_PORT}")
    print(f"Database:      {deps.current_db_path()}")
    uvicorn.run(
        create_app(),
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        log_level="info",
    )
    return 0


__all__ = ["create_app", "main"]
