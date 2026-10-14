"""Rich-based progress reporter and file logger for graph building."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class ReporterConfig:
    """Reporter runtime configuration."""

    enabled_console: bool
    max_nodes: int
    edge_total: int
    relation_mode: str
    llm_total: int | None = None
    text_log_path: str | None = None
    jsonl_log_path: str | None = None
    command_line: str | None = None


class GraphBuildReporter:
    """Human-friendly console reporter with optional persistent log files."""

    def __init__(self, cfg: ReporterConfig):
        self.cfg = cfg
        self.console = Console()
        self.live: Live | None = None
        self.progress: Progress | None = None
        self.node_task_id: int | None = None
        self.edge_task_id: int | None = None
        self.llm_task_id: int | None = None
        self.txt_fp = None
        self.jsonl_fp = None

    def start(self) -> None:
        """Start console progress and open log files."""
        if self.cfg.text_log_path:
            p = Path(self.cfg.text_log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self.txt_fp = p.open("w", encoding="utf-8")
        if self.cfg.jsonl_log_path:
            p = Path(self.cfg.jsonl_log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self.jsonl_fp = p.open("w", encoding="utf-8")

        if self.cfg.enabled_console:
            self.progress = Progress(
                SpinnerColumn(style="bright_cyan"),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=28),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=self.console,
                transient=False,
            )
            self.node_task_id = self.progress.add_task("[cyan]Nodes", total=max(1, self.cfg.max_nodes))
            self.edge_task_id = self.progress.add_task("[magenta]Edges", total=max(1, self.cfg.edge_total))
            if self.cfg.llm_total is not None:
                llm_total = max(0, int(self.cfg.llm_total))
            elif self.cfg.relation_mode == "llm":
                llm_total = self.cfg.edge_total
            elif self.cfg.relation_mode == "llm_batch":
                llm_total = 1
            else:
                llm_total = 0
            self.llm_task_id = self.progress.add_task("[yellow]LLM Calls", total=max(1, llm_total))
            renderables: list[object] = [self.progress]
            if self.cfg.command_line:
                renderables.append(Text(f"Command: {self.cfg.command_line}", style="dim"))
            self.live = Live(
                Group(*renderables),
                console=self.console,
                refresh_per_second=4,
                transient=False,
            )
            self.live.start()
            if llm_total <= 0 and self.llm_task_id is not None:
                self.progress.update(self.llm_task_id, completed=1)

    def stop(self) -> None:
        """Stop progress and close files."""
        if self.live:
            self.live.stop()
            self.live = None
        if self.txt_fp:
            self.txt_fp.close()
            self.txt_fp = None
        if self.jsonl_fp:
            self.jsonl_fp.close()
            self.jsonl_fp = None

    def update_nodes(self, current: int) -> None:
        if self.progress and self.node_task_id is not None:
            self.progress.update(self.node_task_id, completed=current)

    def advance_edge(self, step: int = 1) -> None:
        if self.progress and self.edge_task_id is not None:
            self.progress.advance(self.edge_task_id, step)

    def advance_llm(self, step: int = 1) -> None:
        if self.progress and self.llm_task_id is not None:
            self.progress.advance(self.llm_task_id, step)

    def set_llm_total(self, total: int) -> None:
        """Dynamically update llm progress total."""
        previous_total = self.cfg.llm_total
        self.cfg.llm_total = max(0, int(total))
        if self.progress and self.llm_task_id is not None:
            effective_total = max(1, self.cfg.llm_total)
            update_kwargs: dict[str, int] = {"total": effective_total}
            if (previous_total or 0) <= 0 < self.cfg.llm_total:
                update_kwargs["completed"] = 0
            self.progress.update(self.llm_task_id, **update_kwargs)
            if self.cfg.llm_total <= 0:
                self.progress.update(self.llm_task_id, completed=1)

    def log(
        self,
        stage: str,
        message: str,
        level: str = "info",
        **fields: Any,
    ) -> None:
        """Log to console + txt + jsonl with structured fields."""
        ts = _utc_iso()
        payload = {"ts": ts, "level": level, "stage": stage, "message": message, **fields}
        style = {
            "info": "white",
            "warn": "yellow",
            "error": "bold red",
            "ok": "bold green",
        }.get(level, "white")

        if self.cfg.enabled_console:
            suffix = ""
            if fields:
                kv = " ".join(f"{k}={v}" for k, v in fields.items())
                suffix = f" [dim]{kv}[/dim]"
            self.console.print(f"[{style}]{ts} [{stage}] {message}[/{style}]{suffix}")

        if self.txt_fp:
            suffix = ""
            if fields:
                kv = " ".join(f"{k}={v}" for k, v in fields.items())
                suffix = f" {kv}"
            self.txt_fp.write(f"{ts} [{level}] [{stage}] {message}{suffix}\n")
            self.txt_fp.flush()

        if self.jsonl_fp:
            self.jsonl_fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.jsonl_fp.flush()
