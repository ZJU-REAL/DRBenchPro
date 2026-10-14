"""Public seed-to-case orchestration over retained implementation modules."""
from pathlib import Path
import json
from drbenchpro.data.loader import write_json, write_jsonl
from drbenchpro.data.validation import validate_case
from drbenchpro.llm.config import resolve_model, public_settings
from drbenchpro.common.hashing import content_hash

def export_cases(selected, queries, rubric_payload, edge_inputs, case_prefix):
    qmap = {q["subgraph_id"]: q for q in queries["queries"]}
    rmap = {r["subgraph_id"]: r for r in rubric_payload["subgraph_rubrics"]}
    imap = {r["subgraph_id"]: r for r in edge_inputs["subgraph_edge_rubric_inputs"]}
    cases = []
    for subgraph in selected["selected_subgraphs"]:
        sid = subgraph["subgraph_id"]
        edges = {e["edge_id"]: e for e in subgraph["edges"]}
        inputs = {e["rubric_id"]: e for e in imap[sid]["edge_rubric_inputs"]}
        generated = rmap[sid]["rubrics"]
        edge_to_rubric = {}
        for r in generated:
            edge_id = inputs[r["rubric_id"]]["edge_id"]
            if edge_id in edge_to_rubric:
                raise ValueError("Incomplete edge/rubric correspondence: duplicate edge")
            edge_to_rubric[edge_id] = r["rubric_id"]
        if edge_to_rubric.keys() != edges.keys():
            raise ValueError("Incomplete edge/rubric correspondence")
        rubrics = []
        for r in generated:
            if r.get("llm_rubric_failed"):
                raise ValueError("Rubric generation contains fallback/failed rows; repair before exporting")
            e = edges[inputs[r["rubric_id"]]["edge_id"]]
            depth = int(e["edge_depth"])
            parent_rubric_id = None
            if depth != 1:
                parents = [p["edge_id"] for p in edges.values() if p["target_title"] == e["source_title"] and int(p["edge_depth"]) == depth - 1]
                if len(parents) != 1:
                    raise ValueError("Each non-root rubric requires exactly one parent")
                parent_rubric_id = edge_to_rubric[parents[0]]
            rubrics.append({"rubric_id": r["rubric_id"], "rubric_text": r["rubric_text"], "rubric_depth": depth, "parent_rubric_id": parent_rubric_id, "source_title": e["source_title"], "target_title": e["target_title"]})
        case = {"case_id": f"{case_prefix}-{sid}", "query": qmap[sid]["query"], "rubrics": rubrics}
        validate_case(case)
        if len(rubrics) != len(edges):
            raise ValueError("Incomplete edge/rubric correspondence")
        cases.append(case)
    if not cases:
        raise ValueError("Construction produced no cases")
    return cases

def construct(seed, output, config):
    from .stages.build_graph import build_graph_pipeline
    from .stages.sample_depth1_branch_subgraphs import run_depth1_branch_subgraph_sampling
    from .stages.generate_subgraph_queries import run_subgraph_query_generation
    from .stages.generate_subgraph_edge_rubrics import run_subgraph_edge_rubric_pipeline
    from .subgraphs.depth1_branch_sampling import Depth1BranchSamplingConfig
    from .subgraphs.edge_rubric_materialization import EdgeRubricMaterializationConfig
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Construction output must be empty; use a new output directory")
    settings = resolve_model(config["llm"])
    config = {**config, "llm": settings}
    output.mkdir(parents=True, exist_ok=True)
    identity = content_hash({"seed": seed, "config": public_settings(config)})
    write_json(output / "run.json", {"seed": seed, "config": public_settings(config), "identity": identity, "status": "started"})
    graph = build_graph_pipeline(config, root_title=seed)
    write_json(output / "graph.json", graph)
    run_depth1_branch_subgraph_sampling(output / "graph.json", output, Depth1BranchSamplingConfig(**config.get("sampling", {}), llm_settings=settings))
    generation = config.get("generation", {})
    run_subgraph_query_generation(output / "query_generation_inputs.json", output, query_mode="llm", llm_settings=settings, llm_model_override=settings["model"], query_gen_mode=generation.get("query_gen_mode", "less"), query_prompt_file=generation.get("query_prompt_file", "query_generation_anti_leakage.md"))
    run_subgraph_edge_rubric_pipeline(output / "selected_subgraphs.json", output, EdgeRubricMaterializationConfig(**config.get("evidence", {})), "llm", {**settings, "chunk_size": generation.get("rubric_chunk_size", 32)})
    read = lambda name: json.loads((output / name).read_text())
    cases = export_cases(read("selected_subgraphs.json"), read("subgraph_queries.json"), read("subgraph_rubrics.json"), read("edge_rubric_inputs.json"), "generated-" + identity[:12])
    write_jsonl(output / "cases.jsonl", cases)
    write_jsonl(output / "queries.jsonl", [{"case_id": c["case_id"], "query": c["query"]} for c in cases])
    write_json(output / "run.json", {"seed": seed, "config": public_settings(config), "identity": identity, "status": "complete", "case_count": len(cases)})
    print(json.dumps({"case_count": len(cases), "cases": str(output / "cases.jsonl")}))
