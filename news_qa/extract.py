"""Turn an article page into clean, paragraph-structured text.

Two rules drive this module, both learned from the previous implementation's
failures:

1. **Never lose a space.** ``get_text(strip=True)`` welds inline children to
   their neighbours, so ``<p>See the <a>county report</a> for details</p>``
   became ``See thecounty reportfor details`` -- one fake misspelling per link.
   Every extraction here uses an explicit separator and then repairs the
   punctuation spacing that introduces.

2. **Never fall back to the whole page.** Scraping every ``<p>`` when the
   article selector misses pulls in nav, promos, and "related stories", which
   produces both phantom grammar errors and phantom duplicate paragraphs. An
   article we cannot confidently extract is recorded as a failure so the
   selectors can be fixed, not silently checked as garbage.
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from bs4 import BeautifulSoup, Tag

from .sources import Source

# Tags that never contain article prose.
STRIP_TAGS = [
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "form",
    "button",
    "nav",
    "aside",
    "figure",
    "figcaption",
    "header",
    "footer",
    "template",
]

# Substrings in class/id attributes that mark page furniture rather than prose.
BOILERPLATE_PATTERNS = re.compile(
    r"(related|promo|newsletter|subscribe|advertis|sponsor|share|social|tags?\b|"
    r"most-read|most_read|trending|recirc|read-more|readmore|comments?\b|"
    r"breadcrumb|byline|caption|credit|paywall|inline-cta|outbrain|taboola|"
    # exco-wrapper is WRAL's in-body "Other WRAL Top Stories" recirculation unit
    r"exco|widget|embed|newsletter|jw-player|video-player)",
    re.IGNORECASE,
)

# Block elements that become their own paragraph.
BLOCK_TAGS = ["p", "h2", "h3", "h4", "h5", "blockquote", "li", "pre"]

# Whole paragraphs that are boilerplate even inside a valid article body.
BOILERPLATE_TEXT = re.compile(
    r"^(copyright\b|all rights reserved|sign up for|subscribe to|follow us|"
    r"this story (was|is)|editor'?s note:?$|advertisement$|related:?$|"
    r"more on this|watch:?$|photo(s)? by\b|courtesy of\b|"
    r"(other\s+)?\S*\s*top stories$|top stories$|more from\b|trending\b|"
    r"watch live\b|your browser does not support)",
    re.IGNORECASE,
)

# Minimum characters of body text before we trust an extraction.
MIN_BODY_CHARS = 200

_SPACE_LIKE = {
    0x00A0: " ",  # nbsp
    0x2002: " ",
    0x2003: " ",
    0x2007: " ",
    0x2009: " ",
    0x202F: " ",
    0x205F: " ",
    0x3000: " ",
}
_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD], None)
_CHAR_MAP = {**_SPACE_LIKE, **_INVISIBLE}

# Repairs for spacing the separator introduces around punctuation.
_SPACE_BEFORE_CLOSING = re.compile(r" +([,.;:!?%)\]}…])")
_SPACE_AFTER_OPENING = re.compile(r"([(\[{$#@]) +")
_SPACE_BEFORE_CONTRACTION = re.compile(r" +(['’](?:s|t|re|ve|ll|d|m)\b)", re.IGNORECASE)
_SPACE_AROUND_HYPHEN = re.compile(r"(\w) +([-‐‑]) +(\w)")
_MULTI_SPACE = re.compile(r"[ \t\r\f\v]+")


@dataclass
class Paragraph:
    index: int
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "text": self.text}


@dataclass
class ExtractedArticle:
    url: str
    status: str = "ok"  # 'ok' | 'failed'
    method: str = ""  # 'json-ld' | 'dom'
    note: Optional[str] = None
    title: Optional[str] = None
    byline: Optional[str] = None
    canonical_url: Optional[str] = None
    published_at: Optional[str] = None
    modified_at: Optional[str] = None
    paragraphs: List[Paragraph] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def body_text(self) -> str:
        """The exact text handed to the detectors.

        Built deterministically from the paragraph list so that character
        offsets are reproducible across runs.
        """
        return "\n\n".join(paragraph.text for paragraph in self.paragraphs)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body_text.encode("utf-8")).hexdigest()

    def body_json(self) -> str:
        return json.dumps([paragraph.to_dict() for paragraph in self.paragraphs])


# --- text cleaning --------------------------------------------------------


def clean_text(text: str) -> str:
    """Normalize whitespace and exotic characters without losing word breaks."""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_CHAR_MAP)
    text = _MULTI_SPACE.sub(" ", text)
    return text.strip()


def tidy_spacing(text: str) -> str:
    """Undo the artificial spaces an explicit separator introduces.

    ``get_text(separator=" ")`` correctly separates ``<a>report</a>'s`` into two
    strings, but that yields ``report 's``. These substitutions put punctuation
    back where a reader expects it, without ever re-joining two words.
    """
    text = _SPACE_BEFORE_CONTRACTION.sub(r"\1", text)
    text = _SPACE_BEFORE_CLOSING.sub(r"\1", text)
    text = _SPACE_AFTER_OPENING.sub(r"\1", text)
    text = _SPACE_AROUND_HYPHEN.sub(r"\1\2\3", text)
    return _MULTI_SPACE.sub(" ", text).strip()


def block_to_text(element: Tag) -> str:
    """Extract a block element's text, preserving spaces between inline children."""
    for br in element.find_all("br"):
        br.replace_with("\n")
    raw = element.get_text(separator=" ")
    lines = [clean_text(line) for line in raw.split("\n")]
    return tidy_spacing(" ".join(line for line in lines if line))


# --- DOM preparation ------------------------------------------------------


def _is_boilerplate_tag(element: Tag) -> bool:
    if element.decomposed:
        return False
    identifiers = " ".join(
        [
            " ".join(element.get("class", []) or []),
            element.get("id", "") or "",
            element.get("data-testid", "") or "",
        ]
    )
    return bool(identifiers.strip()) and bool(BOILERPLATE_PATTERNS.search(identifiers))


def prepare_soup(html: str) -> BeautifulSoup:
    """Parse and strip everything that is not prose.

    JSON-LD blocks survive: they are our best source of article body and byline,
    and _collect_paragraphs only reads block tags, so they never leak into prose.
    """
    soup = BeautifulSoup(html, "lxml")

    for tag_name in STRIP_TAGS:
        for element in soup.find_all(tag_name):
            if tag_name == "script" and element.get("type") == "application/ld+json":
                continue
            element.decompose()

    # find_all is materialized first; decomposing invalidates a live iterator,
    # and a decomposed element's parent may already be gone by the time we
    # reach it, hence the decomposed guard above.
    for element in soup.find_all(True):
        if element.name in ("html", "body"):
            continue
        if _is_boilerplate_tag(element):
            element.decompose()

    return soup


def _collect_paragraphs(container: Tag) -> List[str]:
    """Pull block-level prose out of a container, skipping wrapper elements."""
    texts: List[str] = []
    for element in container.find_all(BLOCK_TAGS):
        # A blockquote containing <p> children is a wrapper; take the children.
        if element.find(BLOCK_TAGS):
            continue
        text = block_to_text(element)
        if text and not BOILERPLATE_TEXT.match(text):
            texts.append(text)
    return texts


# --- metadata -------------------------------------------------------------


def _iter_json_ld(soup: BeautifulSoup) -> Iterable[Dict[str, Any]]:
    """Yield every JSON-LD object on the page, flattening @graph and arrays."""
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue

        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.append(node["@graph"])
                yield node


def _article_ld(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
    article_types = {"NewsArticle", "Article", "ReportageNewsArticle", "BlogPosting"}
    for node in _iter_json_ld(soup):
        node_type = node.get("@type")
        types = {node_type} if isinstance(node_type, str) else set(node_type or [])
        if types & article_types:
            return node
    return None


def _author_names(value: Any) -> List[str]:
    """Normalize schema.org author values, which may be a string, dict, or list."""
    names: List[str] = []
    if isinstance(value, str):
        names.append(value)
    elif isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str):
            names.append(name)
    elif isinstance(value, list):
        for item in value:
            names.extend(_author_names(item))
    return [clean_text(name) for name in names if clean_text(name)]


def _meta_content(soup: BeautifulSoup, **attrs: str) -> Optional[str]:
    element = soup.find("meta", attrs=attrs)
    if element and element.get("content"):
        return clean_text(element["content"])
    return None


def extract_byline(soup: BeautifulSoup, ld: Optional[Dict[str, Any]]) -> Optional[str]:
    """Best-effort author identification, JSON-LD first."""
    if ld:
        names = _author_names(ld.get("author"))
        if names:
            return ", ".join(dict.fromkeys(names))

    for attrs in (
        {"property": "article:author"},
        {"name": "author"},
        {"name": "byl"},
        {"property": "og:article:author"},
    ):
        value = _meta_content(soup, **attrs)
        # Some sites put a profile URL here; a URL is not a byline.
        if value and not value.startswith("http"):
            return value

    for selector in (
        "[rel='author']",
        ".byline__name",
        ".byline-name",
        ".author-name",
        "[itemprop='author'] [itemprop='name']",
        "[itemprop='author']",
        ".byline",
    ):
        element = soup.select_one(selector)
        if element:
            text = clean_text(element.get_text(separator=" "))
            text = re.sub(r"^\s*by\s+", "", text, flags=re.IGNORECASE)
            if text and len(text) < 200:
                return text

    return None


# --- entry point ----------------------------------------------------------


def extract_article(html: str, url: str, source: Source) -> ExtractedArticle:
    """Extract structured prose and metadata from an article page."""
    soup = prepare_soup(html)
    ld = _article_ld(soup)

    article = ExtractedArticle(url=url)
    article.canonical_url = None
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        article.canonical_url = canonical["href"]

    article.title = (
        clean_text(ld.get("headline", "")) if ld and isinstance(ld.get("headline"), str) else None
    ) or _meta_content(soup, property="og:title")
    if not article.title and soup.title:
        article.title = clean_text(soup.title.get_text())

    article.byline = extract_byline(soup, ld)
    if ld:
        published = ld.get("datePublished")
        modified = ld.get("dateModified")
        article.published_at = published if isinstance(published, str) else None
        article.modified_at = modified if isinstance(modified, str) else None
    article.published_at = article.published_at or _meta_content(
        soup, property="article:published_time"
    )
    article.modified_at = article.modified_at or _meta_content(
        soup, property="article:modified_time"
    )

    texts = _extract_body(soup, ld, source)
    if texts is None:
        article.status = "failed"
        article.note = "no article body found"
        return article

    article.method, paragraph_texts = texts
    article.paragraphs = [
        Paragraph(index=index, text=text) for index, text in enumerate(paragraph_texts)
    ]

    if len(article.body_text) < MIN_BODY_CHARS:
        article.status = "failed"
        article.note = f"body too short ({len(article.body_text)} chars)"

    return article


def _extract_body(
    soup: BeautifulSoup, ld: Optional[Dict[str, Any]], source: Source
) -> Optional[tuple]:
    """Return (method, [paragraph texts]) or None if nothing usable was found."""
    # 1. JSON-LD articleBody: already plain text, so it sidesteps HTML stripping
    #    entirely and is the most reliable source when present.
    if ld:
        body = ld.get("articleBody")
        if isinstance(body, str):
            cleaned = [clean_text(chunk) for chunk in re.split(r"\n+", body)]
            paragraphs = [
                tidy_spacing(chunk)
                for chunk in cleaned
                if chunk and not BOILERPLATE_TEXT.match(chunk)
            ]
            if sum(len(chunk) for chunk in paragraphs) >= MIN_BODY_CHARS:
                return "json-ld", paragraphs

    # 2. Targeted DOM extraction using this source's selectors.
    for selector in source.article_selectors:
        container = soup.select_one(selector)
        if container is None:
            continue
        paragraphs = _collect_paragraphs(container)
        if sum(len(chunk) for chunk in paragraphs) >= MIN_BODY_CHARS:
            return f"dom:{selector}", paragraphs

    # 3. There is deliberately no whole-page fallback.
    return None
