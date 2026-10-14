"""Constants for the frozen English Wikipedia snapshot."""

from __future__ import annotations

import hashlib
import json

SNAPSHOT_ID = "enwiki-20260701-v1"
DUMP_DATE = "20260701"
DUMP_BASE_URL = f"https://dumps.wikimedia.org/enwiki/{DUMP_DATE}"
DUMP_FILENAME = f"enwiki-{DUMP_DATE}-pages-articles-multistream.xml.bz2"
MULTISTREAM_INDEX_FILENAME = (
    f"enwiki-{DUMP_DATE}-pages-articles-multistream-index.txt.bz2"
)
PAGE_SQL_FILENAME = f"enwiki-{DUMP_DATE}-page.sql.gz"
REDIRECT_SQL_FILENAME = f"enwiki-{DUMP_DATE}-redirect.sql.gz"
CHECKSUMS_FILENAME = f"enwiki-{DUMP_DATE}-sha1sums.txt"
OFFICIAL_SHARD_COUNT = 71
SHARD_MANIFEST_FORMAT = "enwiki-multistream-shards-v1"

SCHEMA_VERSION = 1
BUILDER_VERSION = "wiki-sqlite-fts5-v1"
INDEX_BACKEND = "sqlite-fts5-bm25"
INDEX_VERSION = "v1"


def canonical_index_id(snapshot_id: str) -> str:
    """Return the stable identity for this snapshot/index implementation."""

    normalized = str(snapshot_id or "").strip()
    if not normalized or ":" in normalized:
        raise ValueError("snapshot_id must be non-empty and cannot contain ':'")
    return f"{normalized}-{INDEX_BACKEND}-{INDEX_VERSION}"


INDEX_ID = canonical_index_id(SNAPSHOT_ID)

DEFAULT_CHUNK_CHARS = 2_200
DEFAULT_CHUNK_OVERLAP = 200
DEFAULT_OPEN_CHARS = 4_000
MAX_OPEN_CHARS = 12_000
MAX_SEARCH_RESULTS = 50
MAX_FIND_RESULTS = 50
MAX_SEARCH_QUERY_CHARS = 512
MAX_SOURCE_ID_CHARS = 256
MAX_FIND_PATTERN_CHARS = 256

CANONICAL_INDEX_NAMESPACE = 0
CANONICAL_INDEX_CHUNK_CHARS = DEFAULT_CHUNK_CHARS
CANONICAL_INDEX_CHUNK_OVERLAP = DEFAULT_CHUNK_OVERLAP
CANONICAL_INDEX_CLEANER = "mwparserfromhell"


def index_build_parameters(
    *,
    namespace: int,
    chunk_chars: int,
    chunk_overlap: int,
    cleaner: str,
) -> dict[str, object]:
    """Return the behavior-bearing parameters covered by an index identity."""

    normalized_cleaner = str(cleaner or "").strip()
    if not normalized_cleaner:
        raise ValueError("cleaner must be non-empty")
    return {
        "backend": INDEX_BACKEND,
        "index_version": INDEX_VERSION,
        "namespace": int(namespace),
        "chunk_chars": int(chunk_chars),
        "chunk_overlap": int(chunk_overlap),
        "cleaner": normalized_cleaner,
    }


def index_build_fingerprint(
    *,
    namespace: int,
    chunk_chars: int,
    chunk_overlap: int,
    cleaner: str,
) -> str:
    """Hash index behavior so non-canonical builds cannot reuse the formal ID."""

    payload = index_build_parameters(
        namespace=namespace,
        chunk_chars=chunk_chars,
        chunk_overlap=chunk_overlap,
        cleaner=cleaner,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def index_id_for_build(
    snapshot_id: str,
    *,
    namespace: int,
    chunk_chars: int,
    chunk_overlap: int,
    cleaner: str,
) -> str:
    """Bind an index ID to retrieval-relevant build behavior.

    Canonical builds intentionally retain the published v1 ID. Test and pilot
    builds with different behavior receive a deterministic configuration
    suffix instead of silently claiming the canonical identity.
    """

    parameters = index_build_parameters(
        namespace=namespace,
        chunk_chars=chunk_chars,
        chunk_overlap=chunk_overlap,
        cleaner=cleaner,
    )
    canonical_parameters = index_build_parameters(
        namespace=CANONICAL_INDEX_NAMESPACE,
        chunk_chars=CANONICAL_INDEX_CHUNK_CHARS,
        chunk_overlap=CANONICAL_INDEX_CHUNK_OVERLAP,
        cleaner=CANONICAL_INDEX_CLEANER,
    )
    base = canonical_index_id(snapshot_id)
    if parameters == canonical_parameters:
        return base
    fingerprint = index_build_fingerprint(
        namespace=namespace,
        chunk_chars=chunk_chars,
        chunk_overlap=chunk_overlap,
        cleaner=cleaner,
    )
    return f"{base}-cfg-{fingerprint[:12]}"
