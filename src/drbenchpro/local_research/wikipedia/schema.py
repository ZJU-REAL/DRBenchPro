"""SQLite schema for the immutable Wikipedia runtime index."""

from __future__ import annotations

import sqlite3

from .constants import SCHEMA_VERSION


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS build_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_shards (
    shard_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL,
    dump_filename TEXT NOT NULL,
    index_filename TEXT,
    dump_size INTEGER NOT NULL,
    dump_sha1 TEXT,
    index_sha1 TEXT,
    build_status TEXT NOT NULL DEFAULT 'pending',
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    page_id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    namespace INTEGER NOT NULL,
    revision_timestamp TEXT,
    revision_sha1 TEXT,
    is_redirect INTEGER NOT NULL DEFAULT 0 CHECK (is_redirect IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS pages_title_revision_idx
    ON pages(normalized_title, revision_id, page_id);

CREATE TABLE IF NOT EXISTS redirects (
    page_id INTEGER PRIMARY KEY REFERENCES pages(page_id) ON DELETE CASCADE,
    revision_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    target_title TEXT NOT NULL,
    normalized_target_title TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS redirects_title_idx ON redirects(normalized_title);
CREATE INDEX IF NOT EXISTS redirects_target_idx ON redirects(normalized_target_title);

CREATE TABLE IF NOT EXISTS sections (
    section_id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
    revision_id INTEGER NOT NULL,
    section_index INTEGER NOT NULL,
    heading TEXT NOT NULL,
    anchor TEXT NOT NULL,
    source_id TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    UNIQUE(page_id, section_index)
);

CREATE INDEX IF NOT EXISTS sections_page_idx
    ON sections(page_id, section_index);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY,
    section_id INTEGER NOT NULL REFERENCES sections(section_id) ON DELETE CASCADE,
    page_id INTEGER NOT NULL,
    revision_id INTEGER NOT NULL,
    section_index INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    section TEXT NOT NULL,
    content TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    UNIQUE(section_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_source_order_idx
    ON chunks(page_id, section_index, chunk_index);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title,
    section,
    content,
    content='chunks',
    content_rowid='chunk_id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, title, section, content)
    VALUES (new.chunk_id, new.title, new.section, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, title, section, content)
    VALUES ('delete', old.chunk_id, old.title, old.section, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, title, section, content)
    VALUES ('delete', old.chunk_id, old.title, old.section, old.content);
    INSERT INTO chunks_fts(rowid, title, section, content)
    VALUES (new.chunk_id, new.title, new.section, new.content);
END;
"""


def configure_build_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA cache_size=-262144")


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create all schema objects and reject incompatible database versions."""

    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version not in (0, SCHEMA_VERSION):
        raise RuntimeError(
            f"unsupported Wikipedia DB schema {current_version}; expected {SCHEMA_VERSION}"
        )
    connection.executescript(SCHEMA_SQL)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.commit()
