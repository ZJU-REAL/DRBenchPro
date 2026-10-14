"""Query-only, identity-bound case-level execution and recovery."""
import json
from pathlib import Path
import time

from drbenchpro.common.execution import output_lock, progress, estimate_cost
from drbenchpro.common.hashing import content_hash, file_hash
from drbenchpro.data.loader import read_jsonl, write_json, write_jsonl
from drbenchpro.data.validation import text
from drbenchpro.llm.config import resolve_model, public_settings
from .engine import LocalResearchEngine

CHECKPOINT_VERSION = "research-checkpoint/v1"


def load_queries(path, limit=None, case_ids=None):
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    queries = list(read_jsonl(path))
    seen = set()
    for q in queries:
        if set(q) - {"case_id", "query", "domain", "benchmark_id", "benchmark_version"}:
            raise ValueError("Research requires query-only records; evaluator fields are forbidden")
        text(q.get("case_id"), "case_id"); text(q.get("query"), "query")
        if q["case_id"] in seen:
            raise ValueError("Duplicate query case_id")
        seen.add(q["case_id"])
    if case_ids:
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Duplicate --case-id selection")
        unknown = set(case_ids) - seen
        if unknown:
            raise ValueError(f"Unknown case IDs: {sorted(unknown)}")
        queries = [q for q in queries if q["case_id"] in set(case_ids)]
    queries = queries[:limit]
    if not queries:
        raise ValueError("No queries")
    return queries


def resolved_config(config):
    config = {**config, "model": resolve_model(config["model"])}
    if config.get("model_routes"):
        config["model_routes"] = [resolve_model(route) for route in config["model_routes"]]
    return config


def execution_identity(queries, config, system_id):
    root = Path(__file__).parent
    sources = {name: file_hash(root / name) for name in (
        "agents.py", "config.py", "engine.py", "model_client.py", "runtime.py",
        "wiki_client.py", "citations.py", "schemas.py", "version.py", "runner.py")}
    return content_hash({"checkpoint_version": CHECKPOINT_VERSION, "queries": queries,
                         "config": public_settings(config), "system_id": system_id,
                         "implementation": sources})


def accepted(result):
    return result["status"] in {"success", "truncated"} and bool(result.get("final_report"))


def _compact_attempt(attempt):
    """Keep batch memory bounded; full traces remain in immutable attempt files."""
    result = attempt["result"]
    compact = {key: result[key] for key in ("case_id", "status", "final_report", "error",
                "usage", "tool_usage", "latency_seconds") if key in result}
    compact["trace"] = [{"event_type": event.get("event_type"),
                         "details": {"finish_reason": event.get("details", {}).get("finish_reason")}}
                        for event in result.get("trace", []) if event.get("event_type") in {
                            "model_response", "research_unit_recovered", "final_report_recovered",
                            "research_unit_retry_scheduled"}]
    return {**attempt, "result": compact}


def _summary(queries, attempts):
    latest = {a["result"]["case_id"]: a["result"] for a in attempts}
    complete = sum(accepted(r) and r["status"] == "success" for r in latest.values())
    truncated = sum(accepted(r) and r["status"] == "truncated" for r in latest.values())
    usage = {k: 0 for k in ("input_tokens", "output_tokens", "reasoning_tokens",
                           "cached_input_tokens", "total_tokens", "api_calls")}
    tools = {k: 0 for k in ("search", "open", "find", "total", "successful", "failed")}
    truncated_calls = recoveries = retries = 0
    elapsed = 0.0
    for attempt in attempts:
        result = attempt["result"]
        for key in usage:
            usage[key] += result.get("usage", {}).get(key, 0)
        for key in tools:
            tools[key] += result.get("tool_usage", {}).get(key, 0)
        elapsed += result.get("latency_seconds", 0)
        for event in result.get("trace", []):
            kind = event.get("event_type")
            recoveries += kind in {"research_unit_recovered", "final_report_recovered"}
            retries += kind == "research_unit_retry_scheduled"
            truncated_calls += kind == "model_response" and str(
                event.get("details", {}).get("finish_reason", "")).lower() in {
                    "length", "max_tokens", "max_output_tokens"}
    return {"expected_cases": len(queries), "completed_cases": len(latest),
            "complete_reports": complete, "truncated_reports": truncated,
            "exported_reports": complete + truncated,
            "failed_cases": len(latest) - complete - truncated,
            "pending_cases": len(queries) - len(latest), "case_attempts": len(attempts),
            "retried_case_attempts": len(attempts) - len(latest),
            "model_calls_truncated": truncated_calls, "unit_retries": retries,
            "recovery_events": recoveries, "elapsed_case_seconds": round(elapsed, 6),
            "usage": usage, "tool_usage": tools,
            "accounting": "All persisted case attempts; interrupted in-flight calls may be unreported. "
                          "Token counts are provider-reported; absent usage is not an invoice. "
                          "Reasoning tokens are a subset of output tokens, not additional tokens."}


def research(queries_path, output, config, system_id, limit=None, *, resume=False,
             case_ids=None, retry_failed_only=False, prices=None):
    if retry_failed_only and not resume:
        raise ValueError("--retry-failed-only requires --resume")
    text(system_id, "system_id")
    queries = load_queries(queries_path, limit, case_ids)
    config = resolved_config(config)
    identity = execution_identity(queries, config, system_id)
    output = Path(output)
    with output_lock(output):
        run_path = output / "run.json"
        attempts = []
        attempt_files = {}
        if resume:
            if not run_path.exists():
                raise ValueError("No valid resumable run record; use a new output directory")
            previous = json.loads(run_path.read_text())
            if previous.get("identity") != identity:
                raise ValueError("Resume identity differs: input, selection, model/config, system or implementation changed")
            checkpoint = json.loads((output / "checkpoint.json").read_text())
            if checkpoint.get("identity") != identity:
                raise ValueError("Checkpoint identity differs")
            if not isinstance(checkpoint.get("attempt_files"), dict):
                raise ValueError("Checkpoint is missing attempt integrity metadata")
            for name, digest in checkpoint["attempt_files"].items():
                parts = Path(name).parts
                if not parts or parts[0] != "attempts" or ".." in parts:
                    raise ValueError("Invalid checkpoint attempt path")
                artifact = output / name
                if not artifact.is_file() or file_hash(artifact) != digest:
                    raise ValueError("Missing or modified persisted attempt; refusing to repeat paid work")
            latest_paths = {}
            for path in sorted((output / "attempts").glob("*/*.json")):
                attempt = json.loads(path.read_text())
                result = attempt["result"]
                index = attempt["case_index"]
                if (attempt.get("identity") != identity or not 0 <= index < len(queries)
                        or result.get("case_id") != queries[index]["case_id"]
                        or attempt.get("result_hash") != content_hash(result)
                        or path.relative_to(output).as_posix() != f'attempts/{index:05d}/{attempt["attempt"]:05d}.json'):
                    raise ValueError("Invalid or modified case attempt artifact")
                attempts.append(_compact_attempt(attempt))
                attempt_files[path.relative_to(output).as_posix()] = file_hash(path)
                latest_paths[index] = path
            # Recover derived full-case outputs if the last export was interrupted.
            for index, path in latest_paths.items():
                write_json(output / "runs" / f"{index:05d}.json", json.loads(path.read_text())["result"])
        elif any(p.name != ".runner.lock" for p in output.iterdir()):
            raise ValueError("Research output must be empty; use --resume or a new directory")
        metadata = {"checkpoint_version": CHECKPOINT_VERSION, "identity": identity,
                    "config": public_settings(config), "system_id": system_id,
                    "case_count": len(queries), "input_hash": content_hash(queries),
                    "case_ids": [q["case_id"] for q in queries]}

        def save():
            latest = {a["result"]["case_id"]: a["result"] for a in attempts}
            reports, failures = [], []
            for i, q in enumerate(queries):
                result = latest.get(q["case_id"])
                if result is None:
                    continue
                if accepted(result):
                    reports.append({"system_id": system_id, "case_id": q["case_id"],
                                    "report": result["final_report"], "status": result["status"]})
                else:
                    failures.append({"case_id": q["case_id"], "status": result["status"],
                                     "error_type": (result.get("error") or {}).get("error_type")})
            summary = _summary(queries, attempts)
            summary["cost_estimate"] = estimate_cost(summary["usage"], prices)
            write_jsonl(output / "reports.jsonl", reports)
            write_json(output / "failures.json", failures)
            write_json(output / "checkpoint.json", {"identity": identity,
                       "complete": [r["case_id"] for r in reports], "failures": failures,
                       "attempt_files": attempt_files,
                       "summary": summary})
            write_json(output / "run.json", {**metadata, "summary": summary})
            return latest, summary

        latest, summary = save()
        engine = None
        current_case = [""]
        for i, q in enumerate(queries):
            previous = latest.get(q["case_id"])
            if previous and accepted(previous):
                continue
            if retry_failed_only and previous is None:
                continue
            progress("case_started", stage="research", case_id=q["case_id"],
                     case_position=i + 1, total_cases=len(queries))
            current_case[0] = q["case_id"]
            if engine is None:
                def observe(kind, payload):
                    if kind == "trace_event":
                        progress("research_stage", case_id=current_case[0],
                                 phase=payload.get("phase"), event_type=payload.get("event_type"))
                engine = LocalResearchEngine(config, telemetry_observer=observe)
            started = time.perf_counter()
            try:
                result = engine.run(q["query"], q["case_id"]).to_dict()
            except Exception as exc:
                result = {"case_id": q["case_id"], "query": q["query"], "status": "error",
                          "final_report": "", "error": {"error_type": type(exc).__name__},
                          "latency_seconds": time.perf_counter() - started}
            number = sum(a["case_index"] == i for a in attempts) + 1
            attempt = {"identity": identity, "case_index": i, "attempt": number,
                       "result": result, "result_hash": content_hash(result)}
            attempt_path = output / "attempts" / f"{i:05d}" / f"{number:05d}.json"
            write_json(attempt_path, attempt)
            attempt_files[attempt_path.relative_to(output).as_posix()] = file_hash(attempt_path)
            write_json(output / "runs" / f"{i:05d}.json", result)
            attempts.append(_compact_attempt(attempt))
            latest, summary = save()
            progress("case_finished", stage="research", case_id=q["case_id"],
                     status=result["status"], completed_cases=summary["completed_cases"],
                     complete_reports=summary["complete_reports"], truncated_reports=summary["truncated_reports"],
                     failed_cases=summary["failed_cases"],
                     total_cases=len(queries), elapsed_seconds=result.get("latency_seconds", 0))
        print(json.dumps(summary))
        return 1 if summary["failed_cases"] or summary["pending_cases"] else 0
