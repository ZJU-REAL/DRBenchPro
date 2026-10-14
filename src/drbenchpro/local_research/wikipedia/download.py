"""Resumable downloader and SHA-1 verifier for official dump artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .constants import (
    CHECKSUMS_FILENAME,
    DUMP_BASE_URL,
    DUMP_DATE,
    DUMP_FILENAME,
    MULTISTREAM_INDEX_FILENAME,
    OFFICIAL_SHARD_COUNT,
    PAGE_SQL_FILENAME,
    REDIRECT_SQL_FILENAME,
    SHARD_MANIFEST_FORMAT,
    SNAPSHOT_ID,
)


_DUMP_SHARD_RE = re.compile(
    r"^enwiki-(?P<date>\d{8})-pages-articles-multistream"
    r"(?P<part>\d+)\.xml-p(?P<first>\d+)p(?P<last>\d+)\.bz2$"
)
_INDEX_SHARD_RE = re.compile(
    r"^enwiki-(?P<date>\d{8})-pages-articles-multistream-index"
    r"(?P<part>\d+)\.txt-p(?P<first>\d+)p(?P<last>\d+)\.bz2$"
)


@dataclass(frozen=True)
class OfficialShardPair:
    ordinal: int
    part_number: int
    first_page_id: int
    last_page_id: int
    dump_filename: str
    index_filename: str


def sha1_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_sha1sums(base_url: str, timeout: float = 60.0) -> Dict[str, str]:
    url = f"{base_url.rstrip('/')}/{CHECKSUMS_FILENAME}"
    request = urllib.request.Request(url, headers={"User-Agent": "DRBenchPro-Wiki/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    checksums: Dict[str, str] = {}
    for line in payload.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, filename = parts
        checksums[filename.lstrip("*")] = digest
    return checksums


def discover_shard_pairs(
    checksums: Dict[str, str],
    expected_count: Optional[int] = OFFICIAL_SHARD_COUNT,
) -> List[OfficialShardPair]:
    """Pair official XML and index shards by part and exact page range."""

    dumps: Dict[Tuple[int, int, int], str] = {}
    indexes: Dict[Tuple[int, int, int], str] = {}
    for filename in checksums:
        dump_match = _DUMP_SHARD_RE.fullmatch(filename)
        index_match = _INDEX_SHARD_RE.fullmatch(filename)
        match = dump_match or index_match
        if not match:
            continue
        if match.group("date") != DUMP_DATE:
            continue
        key = (
            int(match.group("part")),
            int(match.group("first")),
            int(match.group("last")),
        )
        (dumps if dump_match else indexes)[key] = filename
    missing_indexes = sorted(set(dumps) - set(indexes))
    missing_dumps = sorted(set(indexes) - set(dumps))
    if missing_indexes or missing_dumps:
        raise RuntimeError(
            "unpaired official shards: "
            f"missing_indexes={missing_indexes[:5]}, missing_dumps={missing_dumps[:5]}"
        )
    keys = sorted(dumps, key=lambda item: (item[1], item[2], item[0]))
    if expected_count is not None and len(keys) != expected_count:
        raise RuntimeError(
            f"expected {expected_count} shard pairs, discovered {len(keys)}"
        )
    pairs: List[OfficialShardPair] = []
    previous_last = 0
    for ordinal, key in enumerate(keys, start=1):
        part, first, last = key
        if first <= previous_last:
            raise RuntimeError("official shard page ranges overlap or are unordered")
        previous_last = last
        pairs.append(
            OfficialShardPair(
                ordinal=ordinal,
                part_number=part,
                first_page_id=first,
                last_page_id=last,
                dump_filename=dumps[key],
                index_filename=indexes[key],
            )
        )
    return pairs


def download_file(
    url: str,
    destination: Path,
    expected_sha1: str,
    timeout: float = 120.0,
    progress: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Download one file via a persistent .part file and verify before rename."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        actual = sha1_file(destination)
        if actual.casefold() == expected_sha1.casefold():
            return {
                "path": str(destination),
                "size_bytes": destination.stat().st_size,
                "sha1": actual,
                "reused": True,
            }
        raise RuntimeError(
            f"existing file has wrong SHA-1: {destination} ({actual})"
        )

    partial = destination.with_name(destination.name + ".part")
    existing_size = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "DRBenchPro-Wiki/1.0"}
    if existing_size:
        headers["Range"] = f"bytes={existing_size}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and partial.is_file():
            actual = sha1_file(partial)
            if actual.casefold() == expected_sha1.casefold():
                os.replace(partial, destination)
                return {
                    "path": str(destination),
                    "size_bytes": destination.stat().st_size,
                    "sha1": actual,
                    "reused": False,
                }
        raise
    with response:
        status = int(getattr(response, "status", response.getcode()))
        if existing_size and status == 206:
            mode = "ab"
        elif status == 200:
            mode = "wb"
            existing_size = 0
        else:
            raise RuntimeError(
                f"server did not honor resume request for {url}: HTTP {status}"
            )
        remaining_header = response.headers.get("Content-Length")
        total_size = (
            existing_size + int(remaining_header)
            if remaining_header and remaining_header.isdigit()
            else None
        )
        transferred = existing_size
        next_report = transferred + 256 * 1024 * 1024
        if progress:
            progress(
                {
                    "filename": destination.name,
                    "downloaded_bytes": transferred,
                    "total_bytes": total_size,
                    "status": "downloading",
                }
            )
        with partial.open(mode) as target:
            while True:
                block = response.read(8 * 1024 * 1024)
                if not block:
                    break
                target.write(block)
                transferred += len(block)
                if progress and transferred >= next_report:
                    progress(
                        {
                            "filename": destination.name,
                            "downloaded_bytes": transferred,
                            "total_bytes": total_size,
                            "status": "downloading",
                        }
                    )
                    next_report = transferred + 256 * 1024 * 1024
            target.flush()
            os.fsync(target.fileno())

    actual = sha1_file(partial)
    if actual.casefold() != expected_sha1.casefold():
        raise RuntimeError(
            f"downloaded SHA-1 mismatch for {destination.name}: "
            f"expected {expected_sha1}, observed {actual}; .part retained"
        )
    os.replace(partial, destination)
    if progress:
        progress(
            {
                "filename": destination.name,
                "downloaded_bytes": destination.stat().st_size,
                "total_bytes": destination.stat().st_size,
                "status": "verified",
            }
        )
    return {
        "path": str(destination),
        "size_bytes": destination.stat().st_size,
        "sha1": actual,
        "reused": False,
    }


def download_snapshot(
    output_dir: Path,
    base_url: str = DUMP_BASE_URL,
    shard_limit: Optional[int] = None,
    combined: bool = False,
    include_supplemental: bool = True,
    workers: int = 4,
    timeout: float = 120.0,
    max_retries: int = 20,
    retry_backoff_seconds: float = 5.0,
    progress: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Download official shard pairs, or the optional combined artifacts."""

    if shard_limit is not None and shard_limit < 1:
        raise ValueError("shard_limit must be positive")
    if workers < 1 or workers > 16:
        raise ValueError("workers must be between 1 and 16")
    if max_retries < 0 or max_retries > 100:
        raise ValueError("max_retries must be between 0 and 100")
    if retry_backoff_seconds < 0:
        raise ValueError("retry_backoff_seconds cannot be negative")
    if combined and shard_limit is not None:
        raise ValueError("shard_limit cannot be combined with combined=True")
    checksums = fetch_sha1sums(base_url, timeout=timeout)
    all_pairs = discover_shard_pairs(checksums)
    pairs = all_pairs[:shard_limit] if shard_limit is not None else all_pairs
    if combined:
        selected = [DUMP_FILENAME, MULTISTREAM_INDEX_FILENAME]
        missing = [filename for filename in selected if filename not in checksums]
        if missing:
            raise RuntimeError(f"official checksum list is missing: {missing}")
        pairs = []

    requested: List[str]
    if combined:
        requested = [
            DUMP_FILENAME,
            MULTISTREAM_INDEX_FILENAME,
        ]
    else:
        requested = []
        for pair in pairs:
            requested.extend((pair.dump_filename, pair.index_filename))
    if include_supplemental:
        requested.extend((PAGE_SQL_FILENAME, REDIRECT_SQL_FILENAME))
    missing = [filename for filename in requested if filename not in checksums]
    if missing:
        raise RuntimeError(f"official checksum list is missing: {missing}")

    def fetch_artifact(filename: str) -> Dict[str, object]:
        attempts = max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                result = download_file(
                    f"{base_url.rstrip('/')}/{filename}",
                    output_dir / filename,
                    checksums[filename],
                    timeout=timeout,
                    progress=progress,
                )
                break
            except Exception as exc:
                if attempt >= attempts:
                    raise
                delay = min(60.0, retry_backoff_seconds * (2 ** (attempt - 1)))
                if progress:
                    progress(
                        {
                            "filename": filename,
                            "status": "retrying",
                            "attempt": attempt,
                            "max_attempts": attempts,
                            "delay_seconds": delay,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:1000],
                        }
                    )
                if delay:
                    time.sleep(delay)
        else:  # pragma: no cover - the loop returns or raises
            raise RuntimeError(f"download attempts exhausted for {filename}")
        # Relative paths keep the manifest usable after mounting the raw directory.
        result["path"] = filename
        record = {
            "filename": filename,
            "url": f"{base_url.rstrip('/')}/{filename}",
            **result,
        }
        return record

    # executor.map preserves the official order in the resulting manifest.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        downloaded = list(executor.map(fetch_artifact, requested))
    records = {
        str(record["filename"]): record for record in downloaded
    }
    files = [records[filename] for filename in requested]
    shards: List[Dict[str, object]] = []

    if combined:
        combined_dump = records[DUMP_FILENAME]
        combined_index = records[MULTISTREAM_INDEX_FILENAME]
    else:
        combined_dump = None
        combined_index = None
        for pair in pairs:
            dump_record = records[pair.dump_filename]
            index_record = records[pair.index_filename]
            shards.append(
                {
                    "shard_id": f"{pair.ordinal:03d}",
                    "ordinal": pair.ordinal,
                    "part_number": pair.part_number,
                    "first_page_id": pair.first_page_id,
                    "last_page_id": pair.last_page_id,
                    "dump": dump_record,
                    "index": index_record,
                }
            )
    manifest = {
        "format": SHARD_MANIFEST_FORMAT,
        "snapshot_id": SNAPSHOT_ID,
        "base_url": base_url,
        "verified_at_unix": int(time.time()),
        "checksum_algorithm": "sha1",
        "complete_snapshot": combined or len(pairs) == len(all_pairs),
        "official_shard_count": len(all_pairs),
        "selected_shard_count": len(pairs),
        "shards": shards,
        "combined": (
            {"dump": combined_dump, "index": combined_index}
            if combined
            else None
        ),
        "supplemental": (
            {
                "page_sql": records[PAGE_SQL_FILENAME],
                "redirect_sql": records[REDIRECT_SQL_FILENAME],
            }
            if include_supplemental
            else {}
        ),
        "files": files,
    }
    manifest_path = output_dir / "download-manifest.json"
    temporary = manifest_path.with_name(manifest_path.name + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    return manifest


def verify_complete_snapshot(
    manifest_path: Path,
    official_checksums: Dict[str, str],
    *,
    verify_file_sha1: bool = True,
    reject_partial_files: bool = True,
) -> Dict[str, object]:
    """Strictly verify the formal 71-pair plus supplemental snapshot.

    ``complete_snapshot`` alone only proves that every XML/index pair was
    selected. Formal deployment also retains both SQL artifacts, so this
    verifier enforces the full 144-file contract and compares it with a fresh
    copy of the dated official checksum registry.
    """

    manifest_path = Path(manifest_path).resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("format") != SHARD_MANIFEST_FORMAT:
        raise ValueError(
            f"unsupported snapshot manifest format: {payload.get('format')!r}"
        )
    if payload.get("snapshot_id") != SNAPSHOT_ID:
        raise ValueError(
            "snapshot manifest identity mismatch: "
            f"{payload.get('snapshot_id')!r} != {SNAPSHOT_ID!r}"
        )
    if payload.get("complete_snapshot") is not True:
        raise ValueError("formal snapshot manifest is not marked complete")
    if int(payload.get("official_shard_count", 0)) != OFFICIAL_SHARD_COUNT:
        raise ValueError("formal snapshot must declare 71 official shard pairs")
    if int(payload.get("selected_shard_count", 0)) != OFFICIAL_SHARD_COUNT:
        raise ValueError("formal snapshot must select all 71 shard pairs")
    if payload.get("combined") is not None:
        raise ValueError("formal snapshot must use the 71-pair sharded layout")

    official_pairs = discover_shard_pairs(official_checksums)
    expected_names: List[str] = []
    for pair in official_pairs:
        expected_names.extend((pair.dump_filename, pair.index_filename))
    expected_names.extend((PAGE_SQL_FILENAME, REDIRECT_SQL_FILENAME))

    shards = payload.get("shards")
    if not isinstance(shards, list) or len(shards) != OFFICIAL_SHARD_COUNT:
        raise ValueError("formal snapshot manifest must contain exactly 71 shards")
    for observed, expected in zip(shards, official_pairs):
        if not isinstance(observed, dict):
            raise ValueError("snapshot shard entry must be an object")
        identity = (
            int(observed.get("ordinal", 0)),
            int(observed.get("part_number", 0)),
            int(observed.get("first_page_id", 0)),
            int(observed.get("last_page_id", 0)),
        )
        expected_identity = (
            expected.ordinal,
            expected.part_number,
            expected.first_page_id,
            expected.last_page_id,
        )
        if identity != expected_identity:
            raise ValueError(
                "snapshot shard identity differs from dated checksum registry: "
                f"observed={identity!r}, expected={expected_identity!r}"
            )
        dump = observed.get("dump")
        index = observed.get("index")
        if not isinstance(dump, dict) or not isinstance(index, dict):
            raise ValueError("snapshot shard is missing dump/index records")
        if dump.get("filename") != expected.dump_filename:
            raise ValueError("snapshot dump filename differs from dated registry")
        if index.get("filename") != expected.index_filename:
            raise ValueError("snapshot index filename differs from dated registry")

    supplemental = payload.get("supplemental")
    if not isinstance(supplemental, dict):
        raise ValueError("formal snapshot is missing supplemental records")
    for key, filename in (
        ("page_sql", PAGE_SQL_FILENAME),
        ("redirect_sql", REDIRECT_SQL_FILENAME),
    ):
        record = supplemental.get(key)
        if not isinstance(record, dict) or record.get("filename") != filename:
            raise ValueError(f"formal snapshot is missing supplemental {filename}")

    files = payload.get("files")
    if not isinstance(files, list) or len(files) != len(expected_names):
        raise ValueError(
            f"formal snapshot must contain {len(expected_names)} file records"
        )
    records: Dict[str, Dict[str, object]] = {}
    observed_order: List[str] = []
    for raw_record in files:
        if not isinstance(raw_record, dict):
            raise ValueError("snapshot file record must be an object")
        filename = str(raw_record.get("filename", "") or "")
        if not filename or filename in records:
            raise ValueError(f"duplicate or empty snapshot filename: {filename!r}")
        records[filename] = raw_record
        observed_order.append(filename)
    if observed_order != expected_names:
        raise ValueError("snapshot file records do not match official dated order")
    for observed, expected in zip(shards, official_pairs):
        if observed["dump"] != records[expected.dump_filename]:
            raise ValueError("snapshot shard dump record differs from files ledger")
        if observed["index"] != records[expected.index_filename]:
            raise ValueError("snapshot shard index record differs from files ledger")
    if supplemental["page_sql"] != records[PAGE_SQL_FILENAME]:
        raise ValueError("page.sql supplemental record differs from files ledger")
    if supplemental["redirect_sql"] != records[REDIRECT_SQL_FILENAME]:
        raise ValueError("redirect.sql supplemental record differs from files ledger")

    if reject_partial_files:
        partials = sorted(path.name for path in manifest_path.parent.glob("*.part"))
        if partials:
            raise ValueError(
                "formal snapshot directory still contains partial files: "
                + ", ".join(partials[:10])
            )

    total_bytes = 0
    for filename in expected_names:
        record = records[filename]
        expected_sha1 = str(official_checksums.get(filename, "") or "").casefold()
        recorded_sha1 = str(record.get("sha1", "") or "").casefold()
        if not expected_sha1 or recorded_sha1 != expected_sha1:
            raise ValueError(
                f"snapshot SHA-1 record differs from dated registry: {filename}"
            )
        raw_path = str(record.get("path") or filename)
        relative_path = Path(raw_path)
        if (
            relative_path.is_absolute()
            or relative_path.name != filename
            or len(relative_path.parts) != 1
        ):
            raise ValueError(f"snapshot artifact path is not portable: {raw_path!r}")
        artifact_path = manifest_path.parent / relative_path
        if not artifact_path.is_file():
            raise FileNotFoundError(artifact_path)
        size = artifact_path.stat().st_size
        if int(record.get("size_bytes", -1)) != size:
            raise ValueError(f"snapshot size record mismatch: {filename}")
        if verify_file_sha1 and sha1_file(artifact_path).casefold() != expected_sha1:
            raise RuntimeError(f"snapshot file SHA-1 mismatch: {filename}")
        total_bytes += size

    return {
        "snapshot_id": SNAPSHOT_ID,
        "complete_snapshot": True,
        "selected_shard_count": OFFICIAL_SHARD_COUNT,
        "file_count": len(expected_names),
        "total_bytes": total_bytes,
        "sha1_verified": bool(verify_file_sha1),
        "partial_file_count": 0,
    }
