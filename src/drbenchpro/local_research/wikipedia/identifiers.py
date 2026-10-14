"""Stable source identifiers and citation URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote


_SOURCE_ID_RE = re.compile(
    r"^(?P<snapshot>[^:]+):p:(?P<page_id>\d+):r:(?P<revision_id>\d+):s:(?P<section_index>\d+)$"
)


@dataclass(frozen=True)
class SourceIdentity:
    snapshot_id: str
    page_id: int
    revision_id: int
    section_index: int


def make_source_id(
    snapshot_id: str,
    page_id: int,
    revision_id: int,
    section_index: int,
) -> str:
    """Create a versioned source identifier that is independent of page titles."""

    if ":" in snapshot_id:
        raise ValueError("snapshot_id cannot contain ':'")
    if min(page_id, revision_id, section_index) < 0:
        raise ValueError("source identifier components must be non-negative")
    return (
        f"{snapshot_id}:p:{page_id}:r:{revision_id}:s:{section_index}"
    )


def parse_source_id(source_id: str) -> SourceIdentity:
    """Parse and validate a stable source identifier."""

    match = _SOURCE_ID_RE.fullmatch(source_id)
    if not match:
        raise ValueError(f"invalid Wikipedia source_id: {source_id!r}")
    return SourceIdentity(
        snapshot_id=match.group("snapshot"),
        page_id=int(match.group("page_id")),
        revision_id=int(match.group("revision_id")),
        section_index=int(match.group("section_index")),
    )


def wikipedia_citation_url(
    page_id: int,
    revision_id: int,
    anchor: str = "",
) -> str:
    """Return an immutable oldid URL for human citation display."""

    url = (
        "https://en.wikipedia.org/w/index.php"
        f"?curid={page_id}&oldid={revision_id}"
    )
    if anchor:
        url += "#" + quote(anchor.replace(" ", "_"), safe="()_,.-")
    return url
