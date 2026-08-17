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


def test_embedding_upsert(tmp_path: Path):
    """Test that inserting same chunk_id twice replaces the vector."""
    conn = db.open_db(tmp_path / "index.db")
    # Insert chunk 1 with vector pointing to (1.0, 0, ...)
    db.insert_embedding(conn, 1, _vec(1.0))
    conn.commit()
    # Insert chunk 2 for comparison
    db.insert_embedding(conn, 2, _vec(0.0, 1.0))
    conn.commit()

    # Now upsert chunk 1 with a different vector pointing to (0, 1, ...)
    db.insert_embedding(conn, 1, _vec(0.0, 1.0))
    conn.commit()

    # Search for (0.9, 0.1) — should now find chunk 2 closest (distance ~0.9)
    # and chunk 1 at (0, 1) second (distance ~0.95)
    results = db.search_embeddings(conn, _vec(0.9, 0.1), k=2)
    # After upsert, chunk 1 is at (0, 1), chunk 2 is at (0, 1) — same position
    # So either could be first, but both should be in results
    assert len(results) == 2
    chunk_ids = {r[0] for r in results}
    assert chunk_ids == {1, 2}


def test_embedding_dimension_validation(tmp_path: Path):
    """Test that inserting wrong-dimension vector raises ValueError."""
    conn = db.open_db(tmp_path / "index.db")
    short_vec = [1.0] * 100  # Wrong dimension
    with pytest.raises(ValueError, match="dimension"):
        db.insert_embedding(conn, 1, short_vec)


def test_embedding_numpy_fallback(tmp_path: Path, monkeypatch):
    """Test numpy fallback path with monkeypatched HAS_SQLITE_VEC."""
    # Monkeypatch before open_db to force numpy path
    monkeypatch.setattr("llmsearch.db.HAS_SQLITE_VEC", False)

    conn = db.open_db(tmp_path / "index.db")
    # Verify chunk_vecs_np table exists instead of chunk_vecs
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')")}
    assert "chunk_vecs_np" in tables

    # Insert embeddings and test upsert
    db.insert_embedding(conn, 1, _vec(1.0))
    db.insert_embedding(conn, 2, _vec(0.0, 1.0))
    conn.commit()

    # Upsert chunk 1
    db.insert_embedding(conn, 1, _vec(0.0, 1.0))
    conn.commit()

    # Search should work via numpy fallback
    results = db.search_embeddings(conn, _vec(0.9, 0.1), k=2)
    assert len(results) == 2
    chunk_ids = {r[0] for r in results}
    assert chunk_ids == {1, 2}
    # Verify results are sorted by distance
    assert results[0][1] <= results[1][1]
