"""Pipeline helpers for depth-1 branch-tree subgraph sampling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from drbenchpro.construction.subgraphs.depth1_branch_sampling import Depth1BranchSamplingConfig, build_depth1_branch_sampling_artifacts
from drbenchpro.common.io import dump_json, ensure_parent_dir, load_json


def _dump_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    ensure_parent_dir(path)
    with Path(path).open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_depth1_branch_subgraph_sampling(
    graph_path: str | Path,
    output_dir: str | Path,
    config: Depth1BranchSamplingConfig,
) -> dict[str, str]:
    """Run depth-1 branch-tree subgraph sampling and write output artifacts."""
    graph_payload = load_json(graph_path)
    artifacts = build_depth1_branch_sampling_artifacts(graph_payload, config)
    artifacts.metadata.source_graph_path = str(Path(graph_path).resolve())

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    candidate_jsonl = output_root / "candidate_subgraphs.jsonl"
    selected_json = output_root / "selected_subgraphs.json"
    query_specs_json = output_root / "query_specs.json"
    query_inputs_json = output_root / "query_generation_inputs.json"
    summary_json = output_root / "sampling_summary.json"

    _dump_jsonl(candidate_jsonl, [candidate.to_dict() for candidate in artifacts.candidates])
    dump_json(
        selected_json,
        {
            "metadata": artifacts.metadata.__dict__,
            "selected_subgraphs": [candidate.to_dict() for candidate in artifacts.selected_subgraphs],
        },
    )
    dump_json(
        query_specs_json,
        {
            "metadata": artifacts.metadata.__dict__,
            "query_specs": [spec.__dict__ for spec in artifacts.query_specs],
        },
    )
    dump_json(
        query_inputs_json,
        {
            "metadata": artifacts.metadata.__dict__,
            "query_generation_inputs": [
                {
                    "subgraph_id": candidate.subgraph_id,
                    "subgraph_type": candidate.subgraph_type,
                    "anchor_node": candidate.anchor_node,
                    "depth_1_node_titles": list(candidate.depth_1_node_titles),
                    "depth_1_node_ids": list(candidate.depth_1_node_ids),
                    "depth_1_branch_selection_reasons": list(candidate.depth_1_branch_selection_reasons),
                    "selection_notes": dict(candidate.selection_notes),
                    "query_spec": candidate.query_spec.__dict__ if candidate.query_spec else None,
                    "query_materials": candidate.query_materials.to_dict() if candidate.query_materials else None,
                }
                for candidate in artifacts.selected_subgraphs
            ],
        },
    )
    dump_json(
        summary_json,
        {
            "metadata": artifacts.metadata.__dict__,
            "summary": dict(artifacts.metadata.summary),
        },
    )
    return {
        "candidate_subgraphs_jsonl": str(candidate_jsonl),
        "selected_subgraphs_json": str(selected_json),
        "query_specs_json": str(query_specs_json),
        "query_generation_inputs_json": str(query_inputs_json),
        "sampling_summary_json": str(summary_json),
    }
