"""Wikipedia HTML parser for intro paragraphs and anchor evidence extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from bs4 import BeautifulSoup, Tag

from drbenchpro.common.ids import make_anchor_id
from drbenchpro.common.text import (
    extract_title_from_href,
    find_first_span,
    is_noise_wikipedia_href,
    normalize_whitespace,
    wikipedia_path_from_href,
)


MIN_PARAGRAPH_LEN = 50
EXCLUDED_PARENT_CLASSES = {
    "infobox",
    "hatnote",
    "thumb",
    "gallery",
    "navbox",
    "metadata",
}
EXCLUDED_ANCESTOR_TAGS = {
    "table",
    "figure",
    "style",
    "script",
    "noscript",
    "math",
}


@dataclass
class ParsedAnchor:
    """Parsed anchor with local evidence coordinates."""

    anchor_id: str
    wikipedia_title: str
    href: str
    text: str
    paragraph_id: int
    start: int
    end: int
    context_text: str
    source_title: str
    source_url: str
    target_url: str
    wikipedia_id: str


@dataclass
class ParsedEntityPage:
    """Parsed page content for graph node creation."""

    wikipedia_title: str
    url: str
    text: list[str] = field(default_factory=list)
    anchors: list[ParsedAnchor] = field(default_factory=list)


class WikipediaParser:
    """Parser focused on main intro paragraphs in Wikipedia article pages."""

    def __init__(self, language: str = "en"):
        self.language = language
        self.base_url = f"https://{language}.wikipedia.org"

    def parse_page(
        self,
        html: str,
        source_title: str,
        source_url: str,
        supplement_anchor_target_count: int | None = None,
        include_supplemental_tables: bool = False,
        collect_all_supplemental_anchors: bool = False,
        include_supplemental_lists: bool = False,
    ) -> ParsedEntityPage:
        """Parse intro paragraphs and valid wiki anchors from one page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        content_root = self._find_main_content_root(soup)
        paragraph_tags = list(self._iter_intro_paragraphs_from_root(content_root)) if content_root else []
        paragraphs: list[str] = []
        anchors: list[ParsedAnchor] = []
        seen_anchor_positions: set[tuple[str, int, int, int]] = set()

        for paragraph_id, p_tag in enumerate(paragraph_tags):
            paragraph_text = self._clean_block_text(p_tag)
            if not paragraph_text:
                continue
            paragraphs.append(paragraph_text)

            for anchor in self._extract_block_anchors(
                block_tag=p_tag,
                paragraph_id=paragraph_id,
                paragraph_text=paragraph_text,
                source_title=source_title,
                source_url=source_url,
            ):
                dedupe_key = (anchor.wikipedia_title, anchor.paragraph_id, anchor.start, anchor.end)
                if dedupe_key in seen_anchor_positions:
                    continue
                seen_anchor_positions.add(dedupe_key)
                anchors.append(anchor)

        if (
            content_root is not None
            and (
                collect_all_supplemental_anchors
                or (
                    supplement_anchor_target_count is not None
                    and self._count_unique_anchor_targets(anchors) < int(supplement_anchor_target_count)
                )
            )
        ):
            next_block_id = len(paragraph_tags)
            used_block_ids = {id(tag) for tag in paragraph_tags}
            supplemental_blocks = self._iter_supplemental_blocks(
                content_root,
                used_block_ids=used_block_ids,
                include_tables=include_supplemental_tables,
                include_lists=include_supplemental_lists,
            )
            for block_tag in supplemental_blocks:
                if (
                    not collect_all_supplemental_anchors
                    and supplement_anchor_target_count is not None
                    and self._count_unique_anchor_targets(anchors) >= int(supplement_anchor_target_count)
                ):
                    break
                block_text = self._clean_block_text(block_tag)
                if not block_text:
                    continue
                for anchor in self._extract_block_anchors(
                    block_tag=block_tag,
                    paragraph_id=next_block_id,
                    paragraph_text=block_text,
                    source_title=source_title,
                    source_url=source_url,
                ):
                    dedupe_key = (anchor.wikipedia_title, anchor.paragraph_id, anchor.start, anchor.end)
                    if dedupe_key in seen_anchor_positions:
                        continue
                    seen_anchor_positions.add(dedupe_key)
                    anchors.append(anchor)
                next_block_id += 1

        return ParsedEntityPage(
            wikipedia_title=source_title,
            url=source_url,
            text=paragraphs,
            anchors=anchors,
        )

    def _iter_intro_paragraphs(self, soup: BeautifulSoup) -> Iterable[Tag]:
        """Yield intro paragraph tags from the first valid paragraph until first section."""
        """函数怎么保证只提取导语区：
            1. 先定位主内容容器（content_root）。
            2. 只遍历这个容器的直接子节点，避免抓到深层噪声。
            3. 只接受 child.name == "p" 的节点（其他标签跳过）。
            4. 过滤掉不合规段落（如短段落、排除容器段落）。
            5. 一旦遇到 h2 且已经开始收集段落（started=True），就 break 停止。
            6. 这一步就是“导语截止线”：第一个章节标题出现后不再抓取。
        """
        content_root = self._find_main_content_root(soup)
        if not content_root:
            return
        for paragraph in self._iter_intro_paragraphs_from_root(content_root):
            yield paragraph

    def _iter_intro_paragraphs_from_root(self, content_root: Tag | None) -> Iterable[Tag]:
        """Yield intro paragraphs from a known parser-output root."""
        if content_root is None:
            return
        direct_matches = list(self._iter_intro_direct_paragraphs(content_root))
        if direct_matches:
            for paragraph in direct_matches:
                yield paragraph
            return
        for paragraph in self._iter_intro_descendant_paragraphs(content_root):
            yield paragraph

    def _iter_intro_direct_paragraphs(self, content_root: Tag) -> Iterable[Tag]:
        """Yield intro paragraphs from direct children of the parser-output root."""
        started = False
        for child in content_root.children:
            if not isinstance(child, Tag):
                continue

            if child.name == "h2":
                if started:
                    break
                continue

            if child.name != "p":
                continue

            if self._is_excluded_paragraph(child):
                continue

            text = self._clean_block_text(child)
            if len(text) < MIN_PARAGRAPH_LEN:
                if started:
                    continue
                continue

            started = True
            yield child

    def _iter_intro_descendant_paragraphs(self, content_root: Tag) -> Iterable[Tag]:
        """Fallback lead-paragraph scan in document order within parser-output."""
        started = False
        for descendant in content_root.descendants:
            if not isinstance(descendant, Tag):
                continue

            if descendant.name == "h2":
                if started:
                    break
                continue

            if descendant.name != "p":
                continue

            if self._is_excluded_paragraph(descendant):
                continue

            text = self._clean_block_text(descendant)
            if len(text) < MIN_PARAGRAPH_LEN:
                if started:
                    continue
                continue

            started = True
            yield descendant

    def _find_main_content_root(self, soup: BeautifulSoup) -> Tag | None:
        """
        Find the main article parser-output container.

        Some pages expose multiple `mw-parser-output` containers (templates, side content).
        We score all parser-output candidates and prefer the one with the richest
        lead-paragraph signal instead of blindly taking the first exact match.
        """
        candidates = soup.find_all(
            "div",
            class_=lambda c: isinstance(c, str) and "mw-parser-output" in c,
        )
        if not candidates:
            return None

        best: Tag | None = None
        best_score: tuple[int, int, int] | None = None
        for candidate in candidates:
            score = self._content_root_score(candidate)
            if best_score is None or score > best_score:
                best = candidate
                best_score = score
        return best

    def _content_root_score(self, candidate: Tag) -> tuple[int, int, int]:
        """Heuristic score for selecting the best parser-output root."""
        classes = set(candidate.get("class", []))
        class_bonus = 1 if {"mw-content-ltr", "mw-parser-output"}.issubset(classes) else 0
        direct_valid = 0
        descendant_valid = 0
        started = False

        for child in candidate.children:
            if not isinstance(child, Tag):
                continue
            if child.name == "h2":
                if started:
                    break
                continue
            if child.name != "p":
                continue
            if self._is_excluded_paragraph(child):
                continue
            text = self._clean_block_text(child)
            if len(text) < MIN_PARAGRAPH_LEN:
                if started:
                    continue
                continue
            started = True
            direct_valid += 1

        started = False
        for descendant in candidate.descendants:
            if not isinstance(descendant, Tag):
                continue
            if descendant.name == "h2":
                if started:
                    break
                continue
            if descendant.name != "p":
                continue
            if self._is_excluded_paragraph(descendant):
                continue
            text = self._clean_block_text(descendant)
            if len(text) < MIN_PARAGRAPH_LEN:
                if started:
                    continue
                continue
            started = True
            descendant_valid += 1

        return (direct_valid, descendant_valid, class_bonus)

    def _is_excluded_paragraph(self, paragraph: Tag) -> bool:
        """Return True when paragraph appears inside known non-main-content containers."""
        current: Tag | None = paragraph
        while isinstance(current, Tag):
            if self._is_excluded_node(current):
                return True
            parent = current.parent
            current = parent if isinstance(parent, Tag) else None
        return False

    def _is_excluded_node(self, node: Tag) -> bool:
        """Return True when a node belongs to excluded content containers."""
        if node.name in EXCLUDED_ANCESTOR_TAGS:
            return True
        classes = set(node.get("class", []))
        return not classes.isdisjoint(EXCLUDED_PARENT_CLASSES)

    def _clean_block_text(self, block: Tag) -> str:
        """Remove citation markers and normalize text from a paragraph or table block."""
        block_copy = BeautifulSoup(str(block), "html.parser").find(block.name or "p")
        if block_copy is None:
            return ""
        for sup in block_copy.find_all("sup"):
            sup.decompose()
        return normalize_whitespace(block_copy.get_text(" ", strip=True))

    def _extract_block_anchors(
        self,
        block_tag: Tag,
        paragraph_id: int,
        paragraph_text: str,
        source_title: str,
        source_url: str,
    ) -> list[ParsedAnchor]:
        """Extract valid anchors from one content block as evidence candidates."""
        results: list[ParsedAnchor] = []

        for a_tag in block_tag.find_all("a", href=True):
            href = str(a_tag.get("href", "")).strip()
            if is_noise_wikipedia_href(href, allowed_language=self.language):
                continue

            anchor_text = normalize_whitespace(a_tag.get_text(" ", strip=True))
            if not anchor_text:
                continue

            target_title = extract_title_from_href(href, allowed_language=self.language)
            if not target_title:
                continue

            target_path = wikipedia_path_from_href(href, allowed_language=self.language).split("#", 1)[0]
            target_url = f"{self.base_url}{target_path}"
            start, end = find_first_span(paragraph_text, anchor_text)
            wikipedia_id = target_title.replace(" ", "_")

            anchor_id = make_anchor_id(
                source_title=source_title,
                target_title=target_title,
                paragraph_id=paragraph_id,
                start=start,
                end=end,
            )

            results.append(
                ParsedAnchor(
                    anchor_id=anchor_id,
                    wikipedia_title=target_title,
                    href=href,
                    text=anchor_text,
                    paragraph_id=paragraph_id,
                    start=start,
                    end=end,
                    context_text=paragraph_text,
                    source_title=source_title,
                    source_url=source_url,
                    target_url=target_url,
                    wikipedia_id=wikipedia_id,
                )
            )
        return results

    def _iter_supplemental_blocks(
        self,
        content_root: Tag,
        *,
        used_block_ids: set[int],
        include_tables: bool,
        include_lists: bool = False,
    ) -> Iterable[Tag]:
        """Yield later article blocks to supplement anchor discovery for sparse pages."""
        intro_started = False
        intro_finished = False
        allowed_names = {"p", "table"}
        if include_lists:
            allowed_names.add("li")
        for descendant in content_root.descendants:
            if not isinstance(descendant, Tag):
                continue
            if descendant.name == "h2":
                if intro_started:
                    intro_finished = True
                continue
            if descendant.name not in allowed_names:
                continue
            if id(descendant) in used_block_ids:
                intro_started = True
                continue
            if descendant.name == "table" and not include_tables:
                continue
            if descendant.name in {"p", "li"} and self._is_excluded_paragraph(descendant):
                continue
            if descendant.name == "table" and self._is_excluded_table_block(descendant):
                continue
            if descendant.name == "table" and not intro_finished:
                # Keep lead-side infobox-style tables out of the supplemental pass.
                continue
            block_text = self._clean_block_text(descendant)
            if len(block_text) < MIN_PARAGRAPH_LEN:
                continue
            yield descendant

    def _count_unique_anchor_targets(self, anchors: list[ParsedAnchor]) -> int:
        """Count unique target titles represented in parsed anchors."""
        return len({anchor.wikipedia_title for anchor in anchors if anchor.wikipedia_title})

    def _is_excluded_table_block(self, table: Tag) -> bool:
        """Return True when a table block should be excluded from supplemental scans."""
        current: Tag | None = table.parent if isinstance(table.parent, Tag) else None
        while isinstance(current, Tag):
            if self._is_excluded_node(current):
                return True
            current = current.parent if isinstance(current.parent, Tag) else None
        return False
