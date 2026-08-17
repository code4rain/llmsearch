from datetime import datetime
from pathlib import Path

from llmsearch import db, indexer
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.models import Document


def make_doc(sid: str, text: str) -> Document:
    return Document(
        source_type="notes", source_id=sid, title=sid, text=text,
        url_or_path=f"/n/{sid}", updated_at=datetime(2026, 8, 1),
    )


def test_index_and_reindex(tmp_path: Path):
    conn = db.open_db(tmp_path / "i.db")
    emb = FakeEmbeddings(dim=768)
    n = indexer.index_documents(conn, [make_doc("a.md", "프로젝트A 킥오프 회의록")], emb)
    assert n == 1
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert chunk_count >= 1
    # 같은 문서 재인덱싱 → 중복 없이 교체
    indexer.index_documents(conn, [make_doc("a.md", "수정된 회의록 본문")], emb)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    row = conn.execute("SELECT text FROM chunks LIMIT 1").fetchone()
    assert "수정된" in row[0]
    # FTS 동기화 확인
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH '수정된'").fetchone()[0] == 1


def test_delete_documents(tmp_path: Path):
    conn = db.open_db(tmp_path / "i.db")
    emb = FakeEmbeddings(dim=768)
    indexer.index_documents(conn, [make_doc("a.md", "본문"), make_doc("b.md", "다른 본문")], emb)
    deleted = indexer.delete_documents(conn, "notes", ["a.md"])
    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH '본문'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH '다른'").fetchone()[0] == 1


def test_sync_state_roundtrip(tmp_path: Path):
    conn = db.open_db(tmp_path / "i.db")
    assert indexer.get_sync_state(conn, "notes") == {}
    indexer.set_sync_state(conn, "notes", {"files": {"a.md": 123.0}})
    assert indexer.get_sync_state(conn, "notes")["files"]["a.md"] == 123.0
