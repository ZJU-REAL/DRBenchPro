"""Text normalization helpers for Wikipedia parsing and generation."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse


WIKIPEDIA_NAMESPACE_PREFIXES = (
    "category:",
    "file:",
    "help:",
    "portal:",
    "special:",
    "template:",
    "talk:",
    "user:",
    "wikipedia:",
    "module:",
    "draft:",
    "book:",
    "timedtext:",
    "mediawiki:",
)


def normalize_whitespace(text: str) -> str:
    """Normalize repeated spaces and newlines into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def normalize_wikipedia_title(raw_title: str) -> str:
    """Normalize Wikipedia titles by decoding and converting underscores."""
    title = unquote(raw_title).replace("_", " ").strip()
    return normalize_whitespace(title)


def wikipedia_path_from_href(href: str, *, allowed_language: str | None = None) -> str:
    """Return a clean /wiki/... path for relative or same-wiki article links."""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("/wiki/"):
        parsed = urlparse(href)
    elif href.startswith("//"):
        parsed = urlparse(f"https:{href}")
    elif href.startswith("http://") or href.startswith("https://"):
        parsed = urlparse(href)
    else:
        return ""

    host = parsed.netloc.lower()
    if host:
        if allowed_language:
            allowed_host = f"{allowed_language.lower()}.wikipedia.org"
            if host not in {allowed_host, "wikipedia.org"}:
                return ""
        elif not (host == "wikipedia.org" or host.endswith(".wikipedia.org")):
            return ""

    query = parse_qs(parsed.query, keep_blank_values=True)
    if "redlink" in query:
        return ""
    if str(query.get("action", [""])[0]).lower() == "edit":
        return ""

    if not parsed.path.startswith("/wiki/"):
        return ""
    return parsed.path


def extract_title_from_href(href: str, *, allowed_language: str | None = None) -> str:
    """Extract wiki title from href like /wiki/Foo#Section."""
    path = wikipedia_path_from_href(href, allowed_language=allowed_language)
    if not path:
        return ""
    raw = path.split("/wiki/", 1)[1].split("#", 1)[0]
    return normalize_wikipedia_title(raw)


def is_noise_wikipedia_href(href: str, *, allowed_language: str | None = None) -> bool:
    """Return True if href is not a clean content-page wiki link."""
    if not href:
        return True
    if href.startswith("#"):
        return True
    path = wikipedia_path_from_href(href, allowed_language=allowed_language)
    if not path:
        return True

    page_part = path.split("/wiki/", 1)[1]
    if not page_part:
        return True

    title_with_fragment = page_part
    title = title_with_fragment.split("#", 1)[0].strip()
    lowered = title.lower()

    if any(lowered.startswith(prefix) for prefix in WIKIPEDIA_NAMESPACE_PREFIXES):
        return True
    return False


def find_first_span(text: str, query: str) -> tuple[int, int]:
    """Find first span of query in text. Returns (-1, -1) if not found."""
    if not text or not query:
        return -1, -1
    start = text.find(query)
    if start >= 0:
        return start, start + len(query)

    match = re.search(re.escape(query), text, flags=re.IGNORECASE)
    if not match:
        return -1, -1
    return match.start(), match.end()


def safe_snippet(text: str, max_chars: int = 240) -> str:
    """Return a bounded snippet."""
    normalized = normalize_whitespace(text)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."
