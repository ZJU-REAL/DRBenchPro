"""Evaluate external reports with identity-bound checkpoints and explicit failure."""
from pathlib import Path
import json
import time
from drbenchpro.common.execution import output_lock, progress, estimate_cost
from drbenchpro.common.hashing import content_hash
from drbenchpro.common.resources import load_prompt_spec
from drbenchpro.data.loader import write_json, write_jsonl
from drbenchpro.data.validation import text, validate_scores
from drbenchpro.llm.config import resolve_model, public_settings
from .judge import evaluate_rubrics_final_llm

def validate_reports(cases, reports):
    seen = set()
    for report in reports:
        cid = report.get("case_id");sid = report.get("system_id", "default")
        text(cid, "case_id");text(sid, "system_id");text(report.get("report"), "report")
        if cid not in cases or (sid, cid) in seen:
            raise ValueError("Unknown or duplicate report case/system")
        seen.add((sid, cid))
    if not reports:
        raise ValueError("No reports")
    systems = sorted({sid for sid, cid in seen})
    return {"reports": len(reports), "systems": [{"system_id": sid,
            "case_count": sum(s == sid for s, cid in seen), "expected_case_count": len(cases),
            "partial": {cid for s, cid in seen if s == sid} != set(cases)} for sid in systems],
            "truncated_reports": sum(r.get("status") == "truncated" for r in reports)}


def evaluate(cases, reports, config, output, resume=False, *, prices=None):
    validate_reports(cases, reports)
    with output_lock(output):
        return _evaluate(cases, reports, config, output, resume, prices)


def _evaluate(cases, reports, config, output, resume=False, prices=None):
    if config.get("output_format") != "score_only_half":
        raise ValueError("Public runner requires score_only_half")
    settings = resolve_model(config["llm"])
    prompt = load_prompt_spec("report_scoring_final_score_only-half.md")
    identity = content_hash({"cases": cases, "reports": reports, "config": public_settings({**config, "llm": settings}), "prompt": vars(prompt)})
    output = Path(output)
    state_path = output / "checkpoint.json"
    state = {"identity": identity, "scores": [], "complete": [], "failures": []}
    if state_path.exists():
        if not resume:
            raise ValueError("Output already has a checkpoint; use --resume or another directory")
        state = json.loads(state_path.read_text())
        if state["identity"] != identity:
            raise ValueError("Resume identity differs: reports, data, prompt or model/config changed")
    elif output.exists() and any(p.name != ".runner.lock" for p in output.iterdir()):
        raise ValueError("Use an empty output directory")
    state.setdefault("statistics", {"api_attempts": 0, "api_errors": 0, "responses_without_usage": 0,
                     "model_calls_truncated": 0, "chunk_fallbacks": 0, "report_attempts": 0,
                     "elapsed_report_seconds": 0.0, "usage": {k: 0 for k in (
                         "input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "cached_input_tokens")}})
    stats = state["statistics"]
    attempts = [int(x) for x in config.get("retry_chunk_sizes", [])]
    if any(x < 1 for x in attempts):
        raise ValueError("retry_chunk_sizes must be positive")
    chunk = int(config.get("chunk_size", 0))
    if chunk < 0:
        raise ValueError("chunk_size must be nonnegative")
    output.mkdir(parents=True, exist_ok=True)
    state["failures"] = []
    def save():
        stats["completed_reports"] = len(state["complete"])
        stats["failed_reports"] = len(state["failures"])
        stats["expected_reports"] = len(reports)
        stats["accounting"] = "Provider-reported usage, including invalid returned batches and retries; " \
                              "absent usage and interrupted calls may be unreported. Reasoning is a subset of output."
        stats["cost_estimate"] = estimate_cost(stats["usage"], prices)
        write_json(state_path, state)
        write_jsonl(output / "scores.jsonl", state["scores"])
        write_json(output / "run.json", {"identity": identity, "judge": public_settings(settings),
                   "prompt_hash": content_hash(vars(prompt)), "complete_reports": len(state["complete"]),
                   "failed_reports": state["failures"], "expected_reports": len(reports), "summary": stats})
    def observe(event):
        if event.get("status") == "cache_hit":
            stats["cache_hits"] = stats.get("cache_hits", 0) + 1
        else:
            stats["api_attempts"] += 1
        stats["api_errors"] += event.get("status") == "error"
        if event.get("status") == "ok":
            stats["responses_without_usage"] += not event.get("usage_reported", False)
            stats["model_calls_truncated"] += str(event.get("finish_reason", "")).lower() in {
                "length", "max_tokens", "max_output_tokens"}
            for source, dest in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"),
                                 ("total_tokens", "total_tokens"), ("reasoning_tokens", "reasoning_tokens"),
                                 ("cached_input_tokens", "cached_input_tokens")):
                stats["usage"][dest] += event.get(source, 0)
        progress("judge_attempt", case_id=current_report[0], status=event.get("status"),
                 attempt=event.get("attempt"), rubric_count=event.get("rubric_count"))
        save()
    def judge_batch(case, report, rubrics):
        try:
            result = evaluate_rubrics_final_llm(case["query"], report["report"], rubrics, settings,
                                              output_format="score_only_half", attempt_hook=observe)
            return result["rubric_results"]
        except Exception:
            size = next((s for s in attempts if s < len(rubrics)), None)
            if size is None:
                raise
            stats["chunk_fallbacks"] += 1
            return [r for i in range(0, len(rubrics), size) for r in judge_batch(case, report, rubrics[i:i+size])]
    current_report = [""]
    save()
    for report in reports:
        cid = report["case_id"];sid = report.get("system_id", "default");key = [sid, cid]
        if key in state["complete"]:
            continue
        current_report[0] = cid
        stats["report_attempts"] += 1
        started = time.perf_counter()
        progress("case_started", stage="evaluate", case_id=cid, system_id=sid,
                 completed_reports=len(state["complete"]), total_reports=len(reports))
        case = cases[cid]
        size = chunk or len(case["rubrics"])
        try:
            rows = [{"system_id": sid, "case_id": cid, "rubric_id": r["rubric_id"], "score": r["score"]} for i in range(0, len(case["rubrics"]), size) for r in judge_batch(case, report, case["rubrics"][i:i+size])]
            validate_scores(case, rows)
        except Exception as exc:
            stats["elapsed_report_seconds"] += time.perf_counter() - started
            state["failures"].append({"system_id": sid, "case_id": cid, "error_type": type(exc).__name__})
            save()
            progress("case_finished", stage="evaluate", case_id=cid, system_id=sid,
                     status="failed", error_type=type(exc).__name__, elapsed_seconds=time.perf_counter() - started)
            continue
        stats["elapsed_report_seconds"] += time.perf_counter() - started
        state["scores"].extend(rows);state["complete"].append(key);save()
        progress("case_finished", stage="evaluate", case_id=cid, system_id=sid,
                 status="completed", completed_reports=len(state["complete"]),
                 total_reports=len(reports), elapsed_seconds=time.perf_counter() - started)
    save()
    return state
