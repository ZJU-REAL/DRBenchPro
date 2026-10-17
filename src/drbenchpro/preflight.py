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
    import json
    from drbenchpro.local_research.model_client import ModelClientError, _is_unrequested_local_tool_response
    from drbenchpro.local_research.wiki_client import WikiToolClient, wikipedia_tool_schemas
    from drbenchpro.local_research.schemas import TokenUsage
    tools = wikipedia_tool_schemas()
    messages = [{"role": "system", "content": "This is a synthetic API compatibility test, not a benchmark run."}]
    usage = TokenUsage()
    repairs = corrections = invalid_calls = 0
    modes = set()
    source_id = f"{engine.config.snapshot_id}:p:1:r:1:s:0"

    def synthetic_transport(url, payload, timeout):
        # Reuse the real Wiki client's argument handling without network I/O.
        result = ({"results": [{"source_id": source_id, "title": "Synthetic archive"}]}
                  if url.endswith("/search") else
                  {"source_id": source_id, "content": "The archive stores research notes."})
        return {"snapshot_id": engine.config.snapshot_id, **result}

    synthetic_wiki = WikiToolClient(
        {"base_url": "http://synthetic.invalid", "max_retries": 0},
        snapshot_id=engine.config.snapshot_id, transport=synthetic_transport,
    )
    for name, instruction in (
        ("search", "Call search with query 'synthetic archive'."),
        ("open", f"Call open with source_id '{source_id}'."),
    ):
        messages.append({"role": "user", "content": instruction})
        for correction in range(2):
            valid = False
            try:
                response = engine.model.chat(messages, tools=tools,
                            tool_choice={"type": "function", "function": {"name": name}},
                            purpose="preflight", max_tokens=max_tokens)
            except ModelClientError as exc:
                # Authentication, network and remote-search policy errors stay terminal.
                if not _is_unrequested_local_tool_response(exc):
                    raise
                for route in exc.usage_by_route:
                    usage.add(TokenUsage(**{k: route.get(k, 0) for k in usage.to_dict()}))
                repairs += sum(bool(a.get("compatibility_fallback")) for a in exc.attempts)
                invalid_calls += 1
            else:
                usage.add(response.usage)
                repairs += response.route.get("thinking_tool_choice_repairs", 0)
                modes.add(response.route.get("tool_choice_mode", "specific"))
                if response.finish_reason.lower() in {"length", "max_tokens", "max_output_tokens", "error"}:
                    raise PreflightError("Synthetic probe was incomplete; inspect provider support or explicitly increase --probe-max-tokens")
                messages.append({"role": "assistant", "content": response.content or None,
                                 **({"tool_calls": [c.to_openai_dict() for c in response.tool_calls]}
                                    if response.tool_calls else {})})
                for call in response.tool_calls:
                    try:
                        if call.name != name or call.parse_error:
                            raise ValueError("Call the requested local tool with JSON object arguments.")
                        if name == "open" and str(call.arguments.get("source_id") or "").strip() != source_id:
                            raise ValueError("Preserve the synthetic source_id returned by search.")
                        result = synthetic_wiki.execute(call.name, call.arguments)
                        valid = True
                    except (ValueError, RuntimeError):
                        invalid_calls += 1
                        result = {"error": "invalid_probe_tool_call",
                                  "message": "Use the requested tool and its declared arguments; preserve returned source IDs."}
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": json.dumps(result)})
            if valid:
                break
            if correction:
                raise PreflightError(f"Model did not complete a valid {name} call after one correction")
            corrections += 1
            messages.append({"role": "user", "content": instruction +
                             " Return a valid tool call, not only text. Use the declared arguments and preserve source IDs."})
    messages.append({"role": "user", "content": "Summarize the synthetic evidence in one sentence. Do not call tools."})
    response = engine.model.chat(messages, tools=None, purpose="preflight", max_tokens=max_tokens)
    usage.add(response.usage)
    if (response.tool_calls or not response.content.strip()
            or response.finish_reason.lower() in {"length", "max_tokens", "max_output_tokens", "error"}):
        raise PreflightError("Model failed the visible-text synthetic probe")
    return {"synthetic_probe": "search/open and visible text passed", "usage": usage.to_dict(),
            "thinking_tool_choice_repairs": repairs, "tool_choice_modes": sorted(modes),
            "tool_corrections": corrections, "invalid_tool_calls": invalid_calls}


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
                     note="External calls may incur charges; synthetic data only; 3 base calls per route, up to 1 correction per tool step and 1 thinking/tool-choice repair per call; no network retries or route fallback")
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
