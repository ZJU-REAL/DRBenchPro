"""Local checks and explicitly requested, bounded synthetic model probes."""
from pathlib import Path
from urllib.parse import urlsplit

from drbenchpro.common.execution import check_writable_directory, progress
from drbenchpro.data.loader import load_cases, read_jsonl
from drbenchpro.llm.config import resolve_model


class PreflightError(ValueError):
    """A selected diagnostic that contains no raw provider response."""


def _endpoint(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise PreflightError("Use an HTTP(S) API base URL without embedded credentials, query or fragment")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        raise PreflightError("Use the API base URL, not the /chat/completions request URL")


def _research_probe(engine, max_tokens):
    from drbenchpro.local_research.wiki_client import wikipedia_tool_schemas
    from drbenchpro.local_research.schemas import TokenUsage
    tools = wikipedia_tool_schemas()
    messages = [{"role": "system", "content": "This is a synthetic API compatibility test, not a benchmark run."}]
    usage = TokenUsage()
    source_id = f"{engine.config.snapshot_id}:p:1:r:1:s:0"
    for name, instruction in (
        ("search", "Call search with query 'synthetic archive'."),
        ("open", f"Call open with source_id '{source_id}'."),
    ):
        messages.append({"role": "user", "content": instruction})
        response = engine.model.chat(messages, tools=tools,
                    tool_choice={"type": "function", "function": {"name": name}},
                    purpose="preflight", max_tokens=max_tokens)
        usage.add(response.usage)
        if response.finish_reason.lower() in {"length", "max_tokens", "max_output_tokens", "error"}:
            raise PreflightError("Synthetic probe was incomplete; inspect provider support or explicitly increase --probe-max-tokens")
        if len(response.tool_calls) != 1 or response.tool_calls[0].name != name:
            raise PreflightError(f"Model did not return the requested {name} tool call")
        call = response.tool_calls[0]
        field = "query" if name == "search" else "source_id"
        if call.parse_error or not isinstance(call.arguments.get(field), str) or not call.arguments[field].strip():
            raise PreflightError(f"Invalid {name} tool arguments")
        if name == "open" and call.arguments[field] != source_id:
            raise PreflightError("Model did not preserve the returned source_id")
        messages.append({"role": "assistant", "content": response.content or None,
                         "tool_calls": [call.to_openai_dict()]})
        result = ({"results": [{"source_id": source_id, "title": "Synthetic archive"}]}
                  if name == "search" else {"source_id": source_id, "content": "The archive stores research notes."})
        import json
        messages.append({"role": "tool", "tool_call_id": call.id,
                         "content": json.dumps({"snapshot_id": engine.config.snapshot_id, **result})})
    messages.append({"role": "user", "content": "Summarize the synthetic evidence in one sentence. Do not call tools."})
    response = engine.model.chat(messages, tools=None, purpose="preflight", max_tokens=max_tokens)
    usage.add(response.usage)
    if (response.tool_calls or not response.content.strip()
            or response.finish_reason.lower() in {"length", "max_tokens", "max_output_tokens", "error"}):
        raise PreflightError("Model failed the visible-text synthetic probe")
    return {"synthetic_probe": "search/open and visible text passed", "usage": usage.to_dict()}


def preflight(stage, config, output, *, queries=None, cases=None, reports=None,
              offline=False, probe_model=False, probe_max_tokens=512):
    if stage not in {"research", "evaluate"}:
        raise PreflightError("Unknown preflight stage")
    if offline and probe_model:
        raise PreflightError("--offline cannot be combined with --probe-model")
    if not 128 <= probe_max_tokens <= 32768:
        raise PreflightError("--probe-max-tokens must be between 128 and 32768")
    status = {"stage": stage, "output": check_writable_directory(output),
              "model_called": False, "wiki_checked": False}
    if Path(output).exists() and any(p.name != ".runner.lock" for p in Path(output).iterdir()):
        raise PreflightError("Preflight expects a fresh output directory; resumable runs use --resume on the job command")
    if stage == "research":
        if cases or reports:
            raise PreflightError("Research preflight accepts --queries, not evaluator cases/reports")
        from drbenchpro.local_research.runner import load_queries, resolved_config
        from drbenchpro.local_research.engine import LocalResearchEngine
        config = resolved_config(config)
        routes = config.get("model_routes") or [config["model"]]
        for route in routes:
            _endpoint(route["base_url"])
        if queries:
            status["query_count"] = len(load_queries(queries))
        # Construction validates configuration but makes no model requests.
        engine = LocalResearchEngine(config)
        if not offline:
            manifest = engine.wiki.validate_index()
            # Separate local diagnostic; never becomes benchmark research evidence.
            hits = engine.wiki.search("research", top_k=1).get("results", [])
            if not hits or not hits[0].get("source_id"):
                raise PreflightError("Wikipedia diagnostic search returned no source; inspect the index")
            opened = engine.wiki.open(hits[0]["source_id"], max_chars=128)
            if not opened.get("content", "").strip():
                raise PreflightError("Wikipedia diagnostic open returned no text")
            status.update(wiki_checked=True, wiki_tools_checked=["search", "open"],
                          snapshot_id=manifest["snapshot_id"], index_id=manifest["index_id"])
        if probe_model:
            progress("model_probe_started", stage=stage,
                     note="External calls may incur charges; synthetic data only; up to 3 requests per route, no retries or fallback")
            probes = []
            for route in routes:
                # Each route is tested explicitly, rather than hidden by a fallback.
                probe_route = {**route, "max_retries": 0, "timeout_seconds": 30,
                               "connect_timeout_seconds": 10}
                probe_engine = LocalResearchEngine({**config, "model": probe_route, "model_routes": []})
                probes.append(_research_probe(probe_engine, probe_max_tokens))
            status.update(model_called=True, probes=probes)
    else:
        if queries or bool(cases) != bool(reports):
            raise PreflightError("Judge preflight requires --cases and --reports together; no --queries")
        if config.get("output_format") != "score_only_half":
            raise PreflightError("Public runner requires score_only_half")
        if int(config.get("chunk_size", 0)) < 0 or any(int(x) < 1 for x in config.get("retry_chunk_sizes", [])):
            raise PreflightError("Invalid Judge chunk configuration")
        settings = resolve_model(config["llm"])
        _endpoint(settings["base_url"])
        if cases:
            from drbenchpro.evaluation.runner import validate_reports
            case_map = load_cases(cases)
            rows = list(read_jsonl(reports))
            validate_reports(case_map, rows)
            status.update(case_count=len(case_map), report_count=len(rows))
        if probe_model:
            from drbenchpro.evaluation.judge import evaluate_rubrics_final_llm
            progress("model_probe_started", stage=stage,
                     note="One external request may incur charges; synthetic data only; no retries or chunk fallback")
            rubric = {"rubric_id": "preflight-r1", "rubric_text": "The archive stores research notes."}
            probe_settings = {**settings, "max_retries": 1, "timeout": 30,
                              "cache_enabled": False, "record_return_info": False,
                              "extra_body": {**settings.get("extra_body", {}), "max_tokens": probe_max_tokens}}
            finishes = []
            result = evaluate_rubrics_final_llm("Describe the synthetic archive.",
                     "The archive stores research notes.", [rubric], probe_settings,
                     attempt_hook=lambda event: finishes.append(str(event.get("finish_reason", "")).lower()),
                     output_format="score_only_half")
            if any(f in {"length", "max_tokens", "max_output_tokens", "error"} for f in finishes):
                raise PreflightError("Judge probe was incomplete; inspect provider support or explicitly increase --probe-max-tokens")
            if result["rubric_results"][0]["score"] != 1:
                raise PreflightError("Judge returned a valid structure but failed the trivial satisfied-rubric probe")
            status.update(model_called=True, synthetic_probe="Judge ID/ternary format and trivial rubric passed")
    return status
