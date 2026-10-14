"""Helpers for loading prompt specifications from markdown files."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


from importlib.resources import files
PROMPTS_DIR = files("drbenchpro").joinpath("resources", "prompts")


@dataclass(frozen=True)
class PromptSpec:
    """Prompt spec loaded from a markdown prompt file."""

    system_prompt: str
    user_prompt_template: str


def to_json_str(value: Any) -> str:
    """Render a Python value as stable JSON text for prompt injection."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


@lru_cache(maxsize=None)
def load_prompt_spec(filename: str) -> PromptSpec:
    """Load and compose prompt sections from a markdown prompt file."""
    category = "evaluation" if filename.startswith("report_scoring") else "construction"
    path = PROMPTS_DIR.joinpath(category, filename)
    text = path.read_text(encoding="utf-8")
    system_prompt = _extract_code_block_after_heading(text, "System prompt")
    user_prompt_template = _extract_code_block_after_heading(text, "User prompt template")
    output_schema = _extract_optional_code_block_after_heading(text, "Output schema")
    requirements = _extract_optional_section_text(text, "Requirements")
    examples = _extract_example_sections(text)
    composed_system_prompt = _compose_system_prompt(
        system_prompt=system_prompt,
        output_schema=output_schema,
        requirements=requirements,
        examples=examples,
    )
    return PromptSpec(
        system_prompt=composed_system_prompt.strip(),
        user_prompt_template=user_prompt_template.strip(),
    )


def render_user_prompt(filename: str, **kwargs: Any) -> str:
    """Render a prompt's user template with the provided kwargs."""
    spec = load_prompt_spec(filename)
    rendered_kwargs = {key: to_json_str(value) for key, value in kwargs.items()}
    return spec.user_prompt_template.format(**rendered_kwargs)


def _extract_code_block_after_heading(markdown_text: str, heading: str) -> str:
    """Extract the first fenced code block following the given markdown heading."""
    section = _extract_section(markdown_text, heading)
    if section is None:
        raise ValueError(f"Heading not found in prompt file: {heading}")
    code_blocks = _extract_fenced_code_blocks(section["body"])
    if not code_blocks:
        raise ValueError(f"No fenced code block found after heading: {heading}")
    return code_blocks[0].strip()


def _extract_optional_code_block_after_heading(markdown_text: str, heading: str) -> str:
    """Extract the first fenced code block after heading, or empty string if absent."""
    section = _extract_section(markdown_text, heading)
    if section is None:
        return ""
    code_blocks = _extract_fenced_code_blocks(section["body"])
    if not code_blocks:
        return ""
    return code_blocks[0].strip()


def _extract_optional_section_text(markdown_text: str, heading: str) -> str:
    """Extract plain section text with fenced code blocks removed, or empty string if absent."""
    section = _extract_section(markdown_text, heading)
    if section is None:
        return ""
    body = re.sub(r"```(?:[A-Za-z0-9_+-]+)?\n.*?```", "", section["body"], flags=re.DOTALL)
    return body.strip()


def _extract_example_sections(markdown_text: str) -> list[tuple[str, str]]:
    """Extract example code blocks from 'Minimal example' or numbered 'Example' sections."""
    examples: list[tuple[str, str]] = []
    for section in _iter_sections(markdown_text):
        title = section["title"]
        if not re.fullmatch(r"(Minimal example|Example(?:\s+\d+)?)", title, flags=re.IGNORECASE):
            continue
        code_blocks = _extract_fenced_code_blocks(section["body"])
        if not code_blocks:
            continue
        examples.append((title, code_blocks[0].strip()))
    return examples


def _compose_system_prompt(
    system_prompt: str,
    output_schema: str = "",
    requirements: str = "",
    examples: list[tuple[str, str]] | None = None,
) -> str:
    """Compose the actual system prompt sent to the LLM from markdown sections."""
    sections = [system_prompt.strip()]
    if output_schema.strip():
        sections.append("Required JSON schema:\n```json\n" + output_schema.strip() + "\n```")
    if requirements.strip():
        sections.append("Additional requirements:\n" + requirements.strip())
    for title, block in examples or []:
        fence_lang = "json" if _looks_like_json(block) else "text"
        sections.append(f"{title}:\n```{fence_lang}\n{block}\n```")
    return "\n\n".join(part for part in sections if part.strip())


def _looks_like_json(text: str) -> bool:
    """Heuristic for choosing a display fence language when re-embedding example blocks."""
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _extract_fenced_code_blocks(text: str) -> list[str]:
    """Return all fenced code block contents from section text."""
    return [
        match.group(1).strip()
        for match in re.finditer(r"```(?:[A-Za-z0-9_+-]+)?\n(.*?)```", text, flags=re.DOTALL)
    ]


def _extract_section(markdown_text: str, heading: str) -> dict[str, str | int] | None:
    """Extract a markdown section body by exact heading title."""
    matches = _heading_matches(markdown_text)
    match = next((item for item in matches if item.group(2).strip() == heading), None)
    if match is None:
        return None
    return _slice_section(markdown_text, match, matches=matches)


def _iter_sections(markdown_text: str) -> list[dict[str, str | int]]:
    """Iterate markdown sections with heading title, level, and body."""
    matches = _heading_matches(markdown_text)
    return [_slice_section(markdown_text, match, matches=matches) for match in matches]


def _slice_section(
    markdown_text: str,
    match: re.Match[str],
    matches: list[re.Match[str]] | None = None,
) -> dict[str, str | int]:
    """Slice one markdown section from its heading until the next same-or-higher heading."""
    if matches is None:
        matches = _heading_matches(markdown_text)
    current_level = len(match.group(1))
    current_start = match.end()
    end = len(markdown_text)
    for candidate in matches:
        if candidate.start() <= match.start():
            continue
        candidate_level = len(candidate.group(1))
        if candidate_level <= current_level:
            end = candidate.start()
            break
    return {
        "title": match.group(2).strip(),
        "level": current_level,
        "body": markdown_text[current_start:end].strip(),
    }


def _heading_matches(markdown_text: str) -> list[re.Match[str]]:
    """Ignore headings inside prompt/example fences (e.g. # Report To Evaluate)."""
    fences = [m.span() for m in re.finditer(
        r"^```[^\n]*\n.*?^```[ \t]*$", markdown_text, flags=re.MULTILINE | re.DOTALL
    )]
    return [m for m in re.finditer(r"^(#+)\s+(.*?)\s*$", markdown_text, flags=re.MULTILINE)
            if not any(start <= m.start() < end for start, end in fences)]
