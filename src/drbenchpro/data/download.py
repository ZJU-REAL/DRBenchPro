"""Verify bundled or separately distributed data, without an invented public URL."""
import json
from pathlib import Path
from drbenchpro.common.hashing import file_hash

def verify_registry(registry):
    registry = Path(registry)
    data = json.loads(registry.read_text())
    root = registry.parent.resolve()
    for name, metadata in data["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Registry file escapes data directory")
        if path.stat().st_size != metadata["bytes"] or file_hash(path) != metadata["sha256"]:
            raise ValueError(f"Integrity mismatch: {name}")
    return {"dataset_id": data["dataset_id"], "verified_files": len(data["files"])}
