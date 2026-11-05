"""HTML to clean, markdown-style text blocks.

Removes scripts, styles, navigation chrome, forms and hidden elements (hidden attribute,
aria-hidden, display:none, visibility:hidden, zero font size or opacity). Hidden text is
a common carrier for prompt injection aimed at agents, so it is dropped rather than returned.
"""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment, Tag

REMOVE_TAGS = [
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
    "nav", "aside", "form", "button", "select", "input", "textarea", "dialog",
]
# Site chrome, removed only when outside the main content (an <article><header> often holds the title).
CHROME_TAGS = ["header", "footer"]
BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote", "td", "th", "dd", "dt", "figcaption"]
HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])",
    re.IGNORECASE,
)
WHITESPACE = re.compile(r"\s+")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
MIN_MAIN_TEXT = 200
MIN_PARAGRAPH = 25
MIN_LIST_ITEM = 15


@dataclass
class Extracted:
    title: str | None
    blocks: list[str]
    published_at: str | None

    @property
    def markdown(self) -> str:
        return "\n\n".join(self.blocks)


def extract(body: bytes | str, charset: str | None = None) -> Extracted:
    if isinstance(body, bytes):
        soup = BeautifulSoup(body, "lxml", from_encoding=charset)
    else:
        soup = BeautifulSoup(body, "lxml")
    title = _clean(soup.title.get_text()) if soup.title else None
    published_at = _published_at(soup)

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    _decompose_all(soup.find_all(REMOVE_TAGS))
    _decompose_all([t for t in soup.find_all(CHROME_TAGS) if t.find_parent(["article", "main"]) is None])
    _decompose_all(soup.find_all(_is_hidden))

    root = _main_root(soup)
    blocks: list[str] = []
    seen: set[str] = set()
    for el in root.find_all(BLOCK_TAGS):
        if el.find(BLOCK_TAGS):
            continue  # use leaf blocks only so nested text is not repeated
        block = _format_block(el)
        if block and block not in seen:
            seen.add(block)
            blocks.append(block)

    if not blocks:
        # Some pages keep all text in <div>s. Fall back to sentence-grouped body text.
        text = _clean(root.get_text(" "))
        blocks = split_text(text, 600) if text else []

    return Extracted(title=title or None, blocks=blocks, published_at=published_at)


def split_text(text: str, target: int) -> list[str]:
    """Split text into chunks of about `target` chars on sentence boundaries."""
    chunks: list[str] = []
    current = ""
    for sentence in SENTENCE_END.split(text):
        while len(sentence) > target:  # very long sentence: hard cut
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:target])
            sentence = sentence[target:]
        if current and len(current) + 1 + len(sentence) > target:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def _clean(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip()


def _decompose_all(tags: list[Tag]) -> None:
    for tag in tags:
        if not tag.decomposed:
            tag.decompose()


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
        return True
    style = tag.get("style")
    return bool(style and HIDDEN_STYLE.search(style))


def _main_root(soup: BeautifulSoup) -> Tag:
    candidates = [soup.find("main"), soup.find(attrs={"role": "main"})]
    articles = soup.find_all("article")
    if len(articles) == 1:
        candidates.append(articles[0])
    for candidate in candidates:
        if candidate is not None and len(candidate.get_text(strip=True)) >= MIN_MAIN_TEXT:
            return candidate
    return soup.body or soup


def _format_block(el: Tag) -> str | None:
    name = el.name
    if name == "pre":
        code = el.get_text().strip("\n")
        return f"```\n{code}\n```" if code.strip() else None
    text = _clean(el.get_text(" "))
    if not text:
        return None
    if len(name) == 2 and name[0] == "h" and name[1].isdigit():
        return "#" * int(name[1]) + " " + text
    if name == "li":
        return f"- {text}" if len(text) >= MIN_LIST_ITEM else None
    if name == "blockquote":
        return f"> {text}"
    return text if len(text) >= MIN_PARAGRAPH else None


def _published_at(soup: BeautifulSoup) -> str | None:
    for attrs in (
        {"property": "article:published_time"},
        {"property": "og:published_time"},
        {"itemprop": "datePublished"},
        {"name": "date"},
        {"name": "pubdate"},
    ):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            return meta["content"].strip()
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        return time_tag["datetime"].strip()
    return None
