import struct
from pathlib import Path

import pytest
from llmsearch import db


def test_open_db_creates_schema(tmp_path: Path):
    conn = db.open_db(tmp_path / "index.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','virtual table') OR type='table'")}
    for t in ("documents", "chunks", "sync_state", "para_map", "meta"):
        assert t in tables, t
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # FTS5 가상 테이블 동작 확인
    conn.execute("INSERT INTO documents(source_type, source_id, title, url_or_path, updated_at) VALUES ('test', 'doc1', 'Test Doc', '/test', '2026-08-17T00:00:00Z')")
    conn.execute("INSERT INTO chunks(doc_id, seq, text) VALUES (1, 0, '프로젝트A 회의록')")
    conn.execute("INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks")
    rows = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '프로젝트A'").fetchall()
    assert len(rows) == 1


def test_schema_version_mismatch(tmp_path: Path):
    p = tmp_path / "index.db"
    conn = db.open_db(p)
    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(db.SchemaMismatchError):
        db.open_db(p)


def _vec(*head: float) -> list[float]:
    """앞자리만 지정한 768차원 벡터 — vec0 float[768] 컬럼과 차원 일치 필수."""
    v = [0.0] * 768
    for i, x in enumerate(head):
        v[i] = x
    return v


def test_embedding_roundtrip(tmp_path: Path):
    conn = db.open_db(tmp_path / "index.db")
    db.insert_embedding(conn, 1, _vec(1.0))
    db.insert_embedding(conn, 2, _vec(0.0, 1.0))
    conn.commit()
    results = db.search_embeddings(conn, _vec(0.9, 0.1), k=2)
    assert results[0][0] == 1  # 가장 가까운 청크가 먼저
    assert len(results) == 2
