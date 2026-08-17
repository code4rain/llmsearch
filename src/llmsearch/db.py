from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import numpy as np

try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

SCHEMA_VERSION = 1
EMBEDDING_DIM = 768


class SchemaMismatchError(Exception):
    """index.db 스키마 버전 불일치 — 인덱스는 소모품이므로 rebuild로 해결한다 (스펙 §8)."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url_or_path TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content_indexed INTEGER NOT NULL DEFAULT 1,
    para_path TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_type, source_id)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id');
CREATE TABLE IF NOT EXISTS sync_state (source_type TEXT PRIMARY KEY, state_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS para_map (source_id TEXT PRIMARY KEY, para_path TEXT NOT NULL, summary_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if HAS_SQLITE_VEC:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.executescript(_SCHEMA)
    if HAS_SQLITE_VEC:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vecs USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{EMBEDDING_DIM}])"
        )
    else:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunk_vecs_np (chunk_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)"
        )
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    elif int(row[0]) != SCHEMA_VERSION:
        conn.close()
        raise SchemaMismatchError(
            f"index.db schema v{row[0]} != v{SCHEMA_VERSION}. index.db를 삭제하고 재인덱싱하세요."
        )
    return conn


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def insert_embedding(conn: sqlite3.Connection, chunk_id: int, vector: list[float]) -> None:
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(f"Vector has {len(vector)} dimensions, expected {EMBEDDING_DIM}")

    if HAS_SQLITE_VEC:
        # vec0 doesn't support INSERT OR REPLACE — delete first then insert
        conn.execute("DELETE FROM chunk_vecs WHERE chunk_id=?", (chunk_id,))
        conn.execute(
            "INSERT INTO chunk_vecs(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, _pack(vector)),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO chunk_vecs_np(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, _pack(vector)),
        )


def delete_embeddings(conn: sqlite3.Connection, chunk_ids: list[int]) -> None:
    table = "chunk_vecs" if HAS_SQLITE_VEC else "chunk_vecs_np"
    conn.executemany(f"DELETE FROM {table} WHERE chunk_id=?", [(c,) for c in chunk_ids])


def search_embeddings(conn: sqlite3.Connection, query: list[float], k: int) -> list[tuple[int, float]]:
    if HAS_SQLITE_VEC:
        rows = conn.execute(
            "SELECT chunk_id, distance FROM chunk_vecs WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (_pack(query), k),
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]
    # numpy 브루트포스 폴백 — 중규모(수십만 청크 미만)에서 충분히 빠름
    rows = conn.execute("SELECT chunk_id, embedding FROM chunk_vecs_np").fetchall()
    if not rows:
        return []
    ids = np.array([r[0] for r in rows])
    mat = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    q = np.asarray(query, dtype=np.float32)
    dists = np.linalg.norm(mat - q, axis=1)
    order = np.argsort(dists)[:k]
    return [(int(ids[i]), float(dists[i])) for i in order]
