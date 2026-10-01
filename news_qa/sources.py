"""Registry of news sources to monitor.

Adding a source is a matter of appending to SOURCES; the scanner and schema are
source-agnostic. ``article_selectors`` are tried in order when JSON-LD metadata
is unavailable or unusable.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    feed_url: str
    homepage: str
    # CSS selectors for the article body, most specific first. There is
    # deliberately no "all <p> on the page" fallback -- see extract.py.
    article_selectors: List[str] = field(default_factory=list)
    active: bool = True


SOURCES = [
    Source(
        key="wral",
        name="WRAL",
        feed_url="https://www.wral.com/news/rss/48/",
        homepage="https://www.wral.com",
        # Verified against live WRAL pages (July 2026). Their NewsArticle
        # JSON-LD carries headline/byline/dates but no articleBody, so the DOM
        # path always runs here.
        article_selectors=[
            "div.article-body",
            "section.article-main",
            "div.story-body",
            "div.article-content",
            "div[data-testid='article-body']",
            "article",
        ],
    ),
]

SOURCES_BY_KEY = {source.key: source for source in SOURCES}


def get_source(key: str) -> Source:
    try:
        return SOURCES_BY_KEY[key]
    except KeyError:
        known = ", ".join(sorted(SOURCES_BY_KEY))
        raise KeyError(f"Unknown source {key!r}. Known sources: {known}") from None
