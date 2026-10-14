"""Strict JSON/JSONL loading; gzip is selected by filename suffix."""
import gzip
import json
from pathlib import Path

def read_jsonl(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for n, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}:{n}: expected an object")
            yield row

def load_cases(path):
    from .validation import validate_case
    path = Path(path)
    rows = [json.loads(path.read_text())] if path.suffix == ".json" else list(read_jsonl(path))
    cases = {}
    for row in rows:
        validate_case(row)
        cid = row["case_id"]
        if cid in cases:
            raise ValueError(f"Duplicate case: {cid}")
        cases[cid] = row
    if not cases:
        raise ValueError("No cases")
    return cases

def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)

def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)
