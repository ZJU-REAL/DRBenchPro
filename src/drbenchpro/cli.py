"""Stable public command interface; algorithm modules are imported lazily."""
import argparse
import json
from pathlib import Path
from importlib.resources import files
from drbenchpro.data.loader import load_cases, read_jsonl, write_json, write_jsonl
from drbenchpro.llm.config import load_config, resolve_model

def main(argv=None):
    parser = argparse.ArgumentParser(prog="drbenchpro")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init-config", help="Copy installed default configurations")
    p.add_argument("--output", required=True, type=Path)
    p = sub.add_parser("validate", help="Validate all case/rubric trees")
    p.add_argument("--cases", required=True)
    p = sub.add_parser("verify-data", help="Check a registry's local file hashes")
    p.add_argument("--registry", required=True, type=Path)
    p = sub.add_parser("validate-reports", help="Validate external reports without model credentials or network")
    p.add_argument("--cases", required=True);p.add_argument("--reports", required=True)
    p.add_argument("--require-full", action="store_true")
    p = sub.add_parser("metrics", help="Compute paper B/D/Overall without a model")
    p.add_argument("--cases", required=True);p.add_argument("--scores", required=True);p.add_argument("--output", required=True)
    p.add_argument("--allow-subset", action="store_true")
    p.add_argument("--csv", help="Export per-system metrics CSV")
    p.add_argument("--case-csv", help="Export per-case metrics CSV")
    p = sub.add_parser("evaluate", help="Judge reports using a configured model")
    p.add_argument("--cases", required=True);p.add_argument("--reports", required=True);p.add_argument("--output", required=True)
    p.add_argument("--config");p.add_argument("--resume", action="store_true")
    p.add_argument("--input-usd-per-million", type=float);p.add_argument("--output-usd-per-million", type=float)
    p = sub.add_parser("research", help="Run Local Research Framework on query-only input")
    p.add_argument("--queries", required=True);p.add_argument("--output", required=True, type=Path)
    p.add_argument("--config");p.add_argument("--system-id", default="local-research");p.add_argument("--limit", type=int)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--case-id", action="append", dest="case_ids", help="Select a case; repeat for multiple IDs")
    p.add_argument("--retry-failed-only", action="store_true", help="With --resume, retry failures but skip unattempted cases")
    p.add_argument("--input-usd-per-million", type=float);p.add_argument("--output-usd-per-million", type=float)
    p = sub.add_parser("preflight", help="Check setup; model calls only with explicit --probe-model")
    p.add_argument("--stage", choices=("research", "evaluate"), required=True)
    p.add_argument("--config");p.add_argument("--output", required=True, type=Path)
    p.add_argument("--queries");p.add_argument("--cases");p.add_argument("--reports")
    p.add_argument("--offline", action="store_true", help="Local validation only; no network calls")
    p.add_argument("--probe-model", action="store_true", help="Make bounded synthetic API calls (may incur charges)")
    p.add_argument("--probe-max-tokens", type=int, default=512)
    p = sub.add_parser("construct", help="Construct new cases from a Wikipedia seed")
    p.add_argument("--seed", required=True);p.add_argument("--output", required=True, type=Path);p.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        if args.command == "init-config":
            args.output.mkdir(parents=True, exist_ok=True)
            sources = sorted(files("drbenchpro").joinpath("resources", "configs").iterdir(), key=lambda p: p.name)
            for source in sources:
                if (args.output / source.name).exists():
                    raise ValueError(f"Refusing to overwrite {source.name}")
            for source in sources:
                (args.output / source.name).write_text(source.read_text())
        elif args.command == "validate":
            cases = load_cases(args.cases)
            print(json.dumps({"cases": len(cases), "rubrics": sum(len(c["rubrics"]) for c in cases.values())}))
        elif args.command == "verify-data":
            from drbenchpro.data.download import verify_registry
            print(json.dumps(verify_registry(args.registry)))
        elif args.command == "validate-reports":
            from drbenchpro.evaluation.runner import validate_reports
            result = validate_reports(load_cases(args.cases), list(read_jsonl(args.reports)))
            if args.require_full and any(s["partial"] for s in result["systems"]):
                raise ValueError("Incomplete report coverage")
            print(json.dumps(result))
        elif args.command == "metrics":
            from drbenchpro.metrics.aggregate import aggregate
            destinations = [Path(p).resolve() for p in (args.output, args.csv, args.case_csv) if p]
            if len(set(destinations)) != len(destinations) or set(destinations) & {Path(args.cases).resolve(), Path(args.scores).resolve()}:
                raise ValueError("Metric outputs must be distinct and must not overwrite inputs")
            result = aggregate(load_cases(args.cases), read_jsonl(args.scores), args.allow_subset)
            write_json(args.output, result)
            from drbenchpro.metrics.export import display
            display(result, args.csv, args.case_csv)
            print(json.dumps(result["systems"]))
        elif args.command == "evaluate":
            from drbenchpro.evaluation.runner import evaluate
            from drbenchpro.common.execution import cost_rates
            prices = cost_rates(args.input_usd_per_million, args.output_usd_per_million)
            state = evaluate(load_cases(args.cases), list(read_jsonl(args.reports)), load_config("evaluation", args.config), args.output, args.resume, prices=prices)
            print(json.dumps({"completed": len(state["complete"]), "failed": len(state["failures"]), "summary": state["statistics"]}))
            return 1 if state["failures"] else 0
        elif args.command == "research":
            from drbenchpro.local_research.runner import research
            from drbenchpro.common.execution import cost_rates
            prices = cost_rates(args.input_usd_per_million, args.output_usd_per_million)
            return research(args.queries, args.output, load_config("local_research", args.config), args.system_id, args.limit,
                            resume=args.resume, case_ids=args.case_ids, retry_failed_only=args.retry_failed_only, prices=prices)
        elif args.command == "preflight":
            from drbenchpro.preflight import preflight, PreflightError
            try:
                result = preflight(args.stage, load_config("local_research" if args.stage == "research" else "evaluation", args.config),
                                   args.output, queries=args.queries, cases=args.cases, reports=args.reports,
                                   offline=args.offline, probe_model=args.probe_model, probe_max_tokens=args.probe_max_tokens)
            except Exception as exc:
                # Upstream exception messages may contain endpoint credentials.
                print(json.dumps({"stage": args.stage, "status": "failed", "error_type": type(exc).__name__,
                                  "detail": str(exc) if isinstance(exc, PreflightError) else "Check failed; verify configuration, required environment variables, input files, output permissions, API compatibility and Wiki identity."}))
                return 1
            print(json.dumps({"status": "passed", **result}))
        elif args.command == "construct":
            from drbenchpro.construction.pipeline import construct
            construct(args.seed, args.output, load_config("construction", args.config))
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")
    return 0
