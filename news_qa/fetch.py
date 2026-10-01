"""Polite HTTP fetching.

This tool watches a newsroom's output; it should never be a burden on their
servers. Every request goes through a single session that identifies itself
honestly, obeys robots.txt, rate-limits per host, backs off on errors, and uses
conditional GETs so unchanged articles cost a 304 instead of a full download.
"""

import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .sources import Source


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str = ""
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and not self.error

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


@dataclass
class FeedEntry:
    title: str
    url: str
    published: Optional[str] = None


class Fetcher:
    """A rate-limited, robots-aware HTTP client scoped to one scan."""

    def __init__(
        self,
        user_agent: str = config.USER_AGENT,
        min_interval: float = config.MIN_REQUEST_INTERVAL,
        timeout: float = config.REQUEST_TIMEOUT,
        respect_robots: bool = config.RESPECT_ROBOTS,
    ):
        self.min_interval = min_interval
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.user_agent = user_agent

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._last_request: Dict[str, float] = {}
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    # --- politeness ------------------------------------------------------

    def _wait_turn(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last_request[host] = time.monotonic()

    def _robots_for(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parsed = urlparse(url)
        host = parsed.netloc
        if host in self._robots:
            return self._robots[host]

        robots_url = urlunparse((parsed.scheme, host, "/robots.txt", "", "", ""))
        parser: Optional[urllib.robotparser.RobotFileParser] = None
        try:
            self._wait_turn(host)
            response = self.session.get(robots_url, timeout=self.timeout)
            if response.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
        except requests.RequestException:
            # An unreachable robots.txt is not permission to ignore it, but it is
            # also not a reason to abandon the scan. Treat it as unrestricted and
            # rely on the rate limit.
            parser = None

        self._robots[host] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    # --- requests --------------------------------------------------------

    def get(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> FetchResult:
        """GET a URL, sending validators so unchanged pages return 304."""
        if not self.allowed(url):
            return FetchResult(url=url, status_code=0, error="blocked by robots.txt")

        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        host = urlparse(url).netloc
        self._wait_turn(host)
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as error:
            return FetchResult(url=url, status_code=0, error=str(error))

        if response.status_code == 304:
            return FetchResult(url=url, status_code=304, etag=etag, last_modified=last_modified)

        if response.status_code != 200:
            return FetchResult(
                url=url,
                status_code=response.status_code,
                error=f"HTTP {response.status_code}",
            )

        return FetchResult(
            url=url,
            status_code=200,
            text=response.text,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )

    def feed(self, source: Source, limit: int = config.MAX_ARTICLES_PER_RUN) -> List[FeedEntry]:
        """Read a source's RSS feed. Returns [] if the feed is unreachable."""
        result = self.get(source.feed_url)
        if not result.ok:
            return []

        parsed = feedparser.parse(result.text)
        entries = []
        for entry in parsed.entries[:limit]:
            link = getattr(entry, "link", None)
            if not link:
                continue
            entries.append(
                FeedEntry(
                    title=getattr(entry, "title", "") or "",
                    url=link,
                    published=getattr(entry, "published", None),
                )
            )
        return entries

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
