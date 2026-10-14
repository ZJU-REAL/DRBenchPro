from __future__ import annotations

import os

import time

from typing import Any, Callable

from drbenchpro.construction.generation.query_generator_llm import QUERY_PROMPT_FILE, generate_query_llm, resolve_query_prompt_file_for_mode

from drbenchpro.construction.generation.query_generator_rule import generate_query_rule

from drbenchpro.llm.client import DEFAULT_LLM_BACKOFF_SECONDS, DEFAULT_LLM_MAX_RETRIES, DEFAULT_LLM_TEMPERATURE, DEFAULT_LLM_TIMEOUT

def build_query_payload(
    graph: dict[str, Any],
    query_mode: str = "rule",
    config: dict[str, Any] | None = None,
    llm_model_override: str | None = None,
    progress_hook: Callable[[str, str, dict[str, Any]], None] | None = None,
    resume_payload: dict[str, Any] | None = None,
    query_gen_mode: str = "less",
    query_prompt_file: str | None = None,
) -> dict[str, Any]:
    """Generate or reuse the benchmark query payload only."""
    def emit(stage: str, message: str, **fields: Any) -> None:
        if progress_hook:
            progress_hook(stage, message, dict(fields))

    def emit_llm_attempt(stage: str, mode: str, event: dict[str, Any]) -> None:
        if str(event.get("status")) != "error":
            return
        error = event.get("error", {}) or {}
        payload = {
            "mode": mode,
            "attempt": int(event.get("attempt", 0)),
            "attempts_total": int(event.get("attempts_total", 0)),
            "elapsed_ms": int(event.get("elapsed_ms", 0)),
            "status_code": error.get("status_code"),
            "error_type": error.get("error_type"),
            "retry_in": (
                f"{float(event.get('next_retry_in_seconds', 0.0)):.1f}s"
                if float(event.get("next_retry_in_seconds", 0.0)) > 0
                else "no_more_retries"
            ),
            "detail": str(error.get("detail", "")),
        }
        for key in ("phase", "edge_id", "source_title", "target_title", "rubric_id", "seed_entity"):
            value = str(event.get(key, "")).strip()
            if value:
                payload[key] = value
        edge_count = int(event.get("edge_count", 0) or 0)
        if edge_count:
            payload["edge_count"] = edge_count
        emit(stage, "LLM attempt failed", **payload)
        if float(event.get("next_retry_in_seconds", 0.0) or 0.0) <= 0:
            emit(
                f"{stage}_terminal",
                "LLM call exhausted retries",
                mode=mode,
                attempt=int(event.get("attempt", 0)),
                attempts_total=int(event.get("attempts_total", 0)),
                elapsed_ms=int(event.get("elapsed_ms", 0)),
                status_code=error.get("status_code"),
                error_type=error.get("error_type"),
                detail=str(error.get("detail", "")),
                edge_id=str(event.get("edge_id", "")).strip(),
                source_title=str(event.get("source_title", "")).strip(),
                target_title=str(event.get("target_title", "")).strip(),
                rubric_id=str(event.get("rubric_id", "")).strip(),
                edge_count=edge_count,
            )

    query_mode = (query_mode or "rule").lower()
    config = config or {}
    resume_payload = resume_payload or {}

    if query_mode not in {"rule", "llm"}:
        raise ValueError(f"Unsupported query mode in MVP: {query_mode}")

    resume_query = str(resume_payload.get("query", "")).strip()
    resume_query_metadata = (
        resume_payload.get("query_metadata", {})
        if isinstance(resume_payload.get("query_metadata", {}), dict)
        else {}
    )
    if resume_query:
        query_payload = {"query": resume_query, **resume_query_metadata}
        emit(
            "query_resume_hit",
            "Using query from partial sample",
            mode=query_mode,
            query_chars=len(resume_query),
        )
    elif query_mode == "llm":
        q_t0 = time.perf_counter()
        generation_cfg = config.get("generation", {}) if isinstance(config.get("generation", {}), dict) else {}
        prompt_filename = resolve_query_prompt_file_for_mode(
            query_prompt_file or generation_cfg.get("query_prompt_file") or QUERY_PROMPT_FILE,
            query_gen_mode=query_gen_mode,
        )
        emit("query_prepare", "Preparing query generation", mode="llm", query_prompt_file=prompt_filename)
        query_llm_settings = _resolve_llm_settings(config, model_key="query", model_override=llm_model_override)
        emit("query_llm_start", "Calling LLM for query", mode="llm", query_prompt_file=prompt_filename)
        query_payload = generate_query_llm(
            graph,
            llm_settings=query_llm_settings,
            attempt_hook=lambda event: emit_llm_attempt("query_llm_attempt_failed", "llm", event),
            query_gen_mode=query_gen_mode,
            query_prompt_file=prompt_filename,
        )
        emit("query_llm_done", "LLM query generated", mode="llm", elapsed_ms=int((time.perf_counter() - q_t0) * 1000))
    else:
        q_t0 = time.perf_counter()
        emit("query_prepare", "Preparing query generation", mode="rule")
        emit("query_rule_start", "Generating query by rules", mode="rule")
        query_payload = generate_query_rule(graph)
        emit("query_rule_done", "Rule query generated", mode="rule", elapsed_ms=int((time.perf_counter() - q_t0) * 1000))
    query = str(query_payload.get("query", "")).strip()
    if not query:
        raise ValueError("Query generation returned empty query.")
    return query_payload

def _resolve_llm_settings(
    config: dict[str, Any],
    model_key: str,
    model_override: str | None = None,
) -> dict[str, Any]:
    """Resolve llm runtime settings from config + env."""
    llm = config.get("llm", {})
    models = llm.get("models", {})
    model = model_override or str(models.get(model_key, ""))
    api_key = str(llm.get("api_key", "")) or os.getenv(str(llm.get("api_key_env", "OPENAI_API_KEY")), "")
    base_url = str(llm.get("base_url", "")) or os.getenv(str(llm.get("base_url_env", "OPENAI_BASE_URL")), "")

    settings = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "temperature": float(llm.get("temperature", DEFAULT_LLM_TEMPERATURE)),
        "omit_temperature": bool(llm.get("omit_temperature", False)),
        "timeout": float(llm.get("timeout", DEFAULT_LLM_TIMEOUT)),
        "max_retries": int(llm.get("max_retries", DEFAULT_LLM_MAX_RETRIES)),
        "backoff_seconds": float(llm.get("backoff_seconds", DEFAULT_LLM_BACKOFF_SECONDS)),
        "concurrency": max(1, int(llm.get("concurrency", 8))),
        "record_return_info": bool(llm.get("record_return_info", False)),
        "return_info_dir": llm.get("return_info_dir") or "",
        "run_tag": llm.get("run_tag") or "",
        "cache_enabled": bool(llm.get("cache_enabled", True)),
        "cache_dir": llm.get("cache_dir") or "",
        "disable_env_proxy": bool(llm.get("disable_env_proxy", True)),
    }
    if not settings["api_key"]:
        raise ValueError("LLM mode requires api key. Set env OPENAI_API_KEY or config llm.api_key.")
    if not settings["base_url"]:
        raise ValueError("LLM mode requires base url. Set env OPENAI_BASE_URL or config llm.base_url.")
    if not settings["model"]:
        raise ValueError(f"LLM mode requires model for '{model_key}'. Set config llm.models.{model_key}.")
    return settings
