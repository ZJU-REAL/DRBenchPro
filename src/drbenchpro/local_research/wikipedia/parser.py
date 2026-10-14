"""Streaming MediaWiki dump parser and deterministic wikitext cleaner."""

from __future__ import annotations

import bz2
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, List, Optional, Tuple


_HEADING_RE = re.compile(
    r"^(?P<marks>={2,6})\s*(?P<title>.*?)\s*(?P=marks)\s*$",
    flags=re.MULTILINE,
)
_PAGE_FRAGMENT_RE = re.compile(
    rb"<page(?:\s[^>]*)?>.*?</page>",
    flags=re.DOTALL,
)


@dataclass(frozen=True)
class DumpPage:
    page_id: int
    revision_id: int
    title: str
    namespace: int
    redirect_target: Optional[str]
    revision_timestamp: Optional[str]
    revision_sha1: Optional[str]
    wikitext: str


@dataclass(frozen=True)
class CleanSection:
    section_index: int
    heading: str
    anchor: str
    content: str


@dataclass(frozen=True)
class TextChunk:
    chunk_index: int
    char_start: int
    char_end: int
    content: str


@dataclass(frozen=True)
class MultistreamRange:
    offset: int
    next_offset: int
    expected_pages: int


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_child(element: ET.Element, name: str) -> Optional[ET.Element]:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _child_text(element: Optional[ET.Element], name: str) -> Optional[str]:
    if element is None:
        return None
    child = _direct_child(element, name)
    return child.text if child is not None else None


def parse_page_element(element: ET.Element) -> DumpPage:
    """Parse one ``<page>`` element without confusing contributor IDs."""

    revision = _direct_child(element, "revision")
    redirect = _direct_child(element, "redirect")
    page_id = _child_text(element, "id")
    revision_id = _child_text(revision, "id")
    if page_id is None or revision_id is None:
        raise ValueError("page is missing page or revision id")
    return DumpPage(
        page_id=int(page_id),
        revision_id=int(revision_id),
        title=_child_text(element, "title") or "",
        namespace=int(_child_text(element, "ns") or 0),
        redirect_target=(redirect.attrib.get("title") if redirect is not None else None),
        revision_timestamp=_child_text(revision, "timestamp"),
        revision_sha1=_child_text(revision, "sha1"),
        wikitext=_child_text(revision, "text") or "",
    )


def _open_dump(path: Path) -> BinaryIO:
    if path.suffix == ".bz2":
        return bz2.open(path, "rb")
    return path.open("rb")


def iter_dump_pages(path: Path) -> Iterator[DumpPage]:
    """Stream pages from a regular XML or XML.bz2 MediaWiki dump."""

    with _open_dump(path) as source:
        context = ET.iterparse(source, events=("start", "end"))
        _, root = next(context)
        for event, element in context:
            if event == "end" and _local_name(element.tag) == "page":
                yield parse_page_element(element)
                element.clear()
                root.clear()


def parse_page_fragments(payload: bytes) -> List[DumpPage]:
    """Parse all page fragments from one independent multistream block."""

    pages: List[DumpPage] = []
    for match in _PAGE_FRAGMENT_RE.finditer(payload):
        pages.append(parse_page_element(ET.fromstring(match.group(0))))
    return pages


def iter_multistream_ranges(index_path: Path, dump_size: int) -> Iterator[MultistreamRange]:
    """Yield compressed byte ranges from an official multistream index."""

    opener = bz2.open if index_path.suffix == ".bz2" else open
    current_offset: Optional[int] = None
    current_count = 0
    with opener(index_path, "rt", encoding="utf-8", errors="strict") as source:
        for line_number, line in enumerate(source, start=1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                offset_text, _page_id, _title = line.split(":", 2)
                offset = int(offset_text)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"invalid multistream index line {line_number}: {line[:120]!r}"
                ) from exc
            if current_offset is None:
                current_offset = offset
                current_count = 1
            elif offset == current_offset:
                current_count += 1
            else:
                if offset <= current_offset:
                    raise ValueError("multistream offsets must be strictly increasing")
                yield MultistreamRange(current_offset, offset, current_count)
                current_offset = offset
                current_count = 1
    if current_offset is not None:
        if current_offset >= dump_size:
            raise ValueError("last multistream offset is outside the dump")
        yield MultistreamRange(current_offset, dump_size, current_count)


def read_multistream_block(source: BinaryIO, block: MultistreamRange) -> List[DumpPage]:
    """Read and decompress exactly one independently compressed stream."""

    source.seek(block.offset)
    compressed = source.read(block.next_offset - block.offset)
    payload = bz2.decompress(compressed)
    pages = parse_page_fragments(payload)
    if len(pages) != block.expected_pages:
        raise ValueError(
            "multistream page count mismatch at offset "
            f"{block.offset}: expected {block.expected_pages}, parsed {len(pages)}"
        )
    return pages


def split_wikitext_sections(wikitext: str) -> List[Tuple[int, str, str]]:
    """Split wikitext into lead and heading-delimited sections."""

    sections: List[Tuple[int, str, str]] = []
    cursor = 0
    heading = "Lead"
    index = 0
    for match in _HEADING_RE.finditer(wikitext):
        sections.append((index, heading, wikitext[cursor : match.start()]))
        index += 1
        heading = match.group("title").strip() or f"Section {index}"
        cursor = match.end()
    sections.append((index, heading, wikitext[cursor:]))
    return sections


def _template_values(match: re.Match[str]) -> str:
    body = match.group(1)
    values: List[str] = []
    for part in body.split("|")[1:]:
        value = part.split("=", 1)[-1].strip()
        if value:
            values.append(value)
    return " ".join(values)


def fallback_clean_wikitext(text: str) -> str:
    """Conservative cleaner used when mwparserfromhell is unavailable or fails."""

    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<ref\b[^>]*>.*?</ref\s*>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<ref\b[^>]*/\s*>", " ", text, flags=re.I)
    text = re.sub(r"\[\[(?:File|Image|Category):.*?\]\]", " ", text, flags=re.I | re.S)
    text = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[(?:https?|ftp)://\S+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[(?:https?|ftp)://[^\]]+\]", " ", text)
    for _ in range(24):
        updated = re.sub(r"\{\{([^{}]*)\}\}", _template_values, text)
        if updated == text:
            break
        text = updated
    text = re.sub(r"\{\|", "\n", text)
    text = re.sub(r"\|\}", "\n", text)
    text = re.sub(r"(?m)^\s*\|-.*$", "\n", text)
    text = re.sub(r"(?m)^\s*[!|]\s*", "", text)
    text = re.sub(r"\|\||!!", " | ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"__\w+__", " ", text)
    return normalize_plain_text(html.unescape(text))


def normalize_plain_text(text: str) -> str:
    """Normalize whitespace while preserving paragraph boundaries."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    paragraphs: List[str] = []
    current: List[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs).strip()


class WikitextCleaner:
    """Use mwparserfromhell when installed, with a per-section fallback."""

    def __init__(self, prefer_mwparser: bool = True) -> None:
        self._mwparser = None
        if prefer_mwparser:
            try:
                import mwparserfromhell  # type: ignore
            except ImportError:
                pass
            else:
                self._mwparser = mwparserfromhell
        self.backend = "mwparserfromhell" if self._mwparser else "fallback"

    def clean(self, text: str) -> str:
        if self._mwparser is not None:
            try:
                code = self._mwparser.parse(text)
                stripped = code.strip_code(
                    normalize=True,
                    collapse=True,
                    keep_template_params=True,
                )
                return normalize_plain_text(stripped)
            except Exception:
                # A malformed page must not abort a multi-million-page build.
                return fallback_clean_wikitext(text)
        return fallback_clean_wikitext(text)


def clean_sections(wikitext: str, cleaner: WikitextCleaner) -> List[CleanSection]:
    """Clean each section independently and discard empty markup-only sections."""

    output: List[CleanSection] = []
    for section_index, heading, raw in split_wikitext_sections(wikitext):
        content = cleaner.clean(raw)
        if not content:
            continue
        clean_heading = fallback_clean_wikitext(heading) or heading.strip()
        output.append(
            CleanSection(
                section_index=section_index,
                heading=clean_heading,
                anchor="" if section_index == 0 else clean_heading,
                content=content,
            )
        )
    return output


def chunk_text(text: str, max_chars: int, overlap_chars: int) -> List[TextChunk]:
    """Create deterministic, bounded chunks with paragraph-aware boundaries."""

    if max_chars < 256:
        raise ValueError("max_chars must be at least 256")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be in [0, max_chars)")
    chunks: List[TextChunk] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        while start < text_length and text[start].isspace():
            start += 1
        if start >= text_length:
            break
        hard_end = min(text_length, start + max_chars)
        end = hard_end
        if hard_end < text_length:
            minimum = start + max(max_chars // 2, 128)
            candidates = [
                text.rfind("\n\n", minimum, hard_end),
                text.rfind(". ", minimum, hard_end),
                text.rfind("; ", minimum, hard_end),
                text.rfind(" ", minimum, hard_end),
            ]
            boundary = max(candidates)
            if boundary >= minimum:
                end = boundary + (2 if text[boundary : boundary + 2] in {". ", "; "} else 0)
        while end > start and text[end - 1].isspace():
            end -= 1
        if end <= start:
            end = hard_end
        chunks.append(
            TextChunk(
                chunk_index=len(chunks),
                char_start=start,
                char_end=end,
                content=text[start:end],
            )
        )
        if end >= text_length:
            break
        next_start = max(start + 1, end - overlap_chars)
        while next_start < end and not text[next_start].isspace():
            next_start += 1
        start = next_start
    return chunks
