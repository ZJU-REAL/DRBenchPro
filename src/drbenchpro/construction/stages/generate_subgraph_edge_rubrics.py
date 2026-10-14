"""Pipeline entry for subgraph edge evidence materialization and rubric generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from drbenchpro.construction.generation.subgraph_edge_rubric_llm import (
    generate_subgraph_edge_rubrics_llm,
    generate_subgraph_edge_rubrics_rule,
)
from drbenchpro.construction.subgraphs.edge_rubric_materialization import (
    EdgeRubricMaterializationConfig,
    materialize_subgraph_edge_rubric_inputs,
)
from drbenchpro.common.io import dump_json, load_json


def run_subgraph_edge_rubric_pipeline(
    subgraphs_path: str | Path,
    output_dir: str | Path,
    materialization_config: EdgeRubricMaterializationConfig,
    rubric_mode: str,
    llm_settings: dict[str, Any],
    precomputed_edge_inputs_payload: dict[str, Any] | None = None,
    materialization_progress_hook: Callable[[dict[str, Any]], None] | None = None,
    rubric_progress_hook: Callable[[dict[str, Any]], None] | None = None,
    attempt_hook: Callable[[dict[str, Any]], None] | None = None,
    resume_payload: dict[str, Any] | None = None,
    partial_hook: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    retry_fallback_rubrics: bool = False,
) -> dict[str, str]:
    """Materialize wiki evidence for sampled subgraph edges, then generate rubrics."""
    edge_inputs_payload = precomputed_edge_inputs_payload
    if edge_inputs_payload is None:
        subgraphs_payload = load_json(subgraphs_path)
        edge_inputs_payload = materialize_subgraph_edge_rubric_inputs(
            subgraphs_payload,
            cfg=materialization_config,
            progress_hook=materialization_progress_hook,
        )
    edge_inputs_payload.setdefault("metadata", {})["source_subgraphs_path"] = str(Path(subgraphs_path).resolve())

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    edge_inputs_json = output_root / "edge_rubric_inputs.json"
    subgraph_rubrics_json = output_root / "subgraph_rubrics.json"

    dump_json(edge_inputs_json, edge_inputs_payload)

    if rubric_mode == "llm":
        rubric_payload = generate_subgraph_edge_rubrics_llm(
            edge_inputs_payload=edge_inputs_payload,
            llm_settings=llm_settings,
            attempt_hook=attempt_hook,
            progress_hook=rubric_progress_hook,
            resume_payload=resume_payload,
            partial_hook=partial_hook,
            retry_fallback_rubrics=retry_fallback_rubrics,
        )
    elif rubric_mode == "rule":
        rubric_payload = generate_subgraph_edge_rubrics_rule(
            edge_inputs_payload,
            progress_hook=rubric_progress_hook,
            resume_payload=resume_payload,
            partial_hook=partial_hook,
        )
    else:
        raise ValueError(f"Unsupported rubric mode: {rubric_mode}")

    dump_json(subgraph_rubrics_json, rubric_payload)
    return {
        "edge_rubric_inputs_json": str(edge_inputs_json),
        "subgraph_rubrics_json": str(subgraph_rubrics_json),
    }
