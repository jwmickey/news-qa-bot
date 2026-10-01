from . import articles, dashboard, issues, scans, settings

ROUTERS = [
    dashboard.router,
    scans.router,
    issues.router,
    articles.router,
    settings.router,
]

__all__ = ["ROUTERS"]
