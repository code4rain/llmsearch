from datetime import datetime
from pathlib import Path

from llmsearch import db, indexer
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.eval.golden import evaluate
from llmsearch.models import Document


def test_evaluate(tmp_path: Path):
    conn = db.open_db(tmp_path / "g.db")
    emb = FakeEmbeddings(dim=768)
    indexer.index_documents(conn, [
        Document("notes", "kick.md", "프로젝트A 킥오프", "프로젝트A 킥오프 회의록 8월 1일",
                 "/n/kick.md", datetime(2026, 8, 1)),
        Document("notes", "lunch.md", "점심", "김치찌개", "/n/lunch.md", datetime(2026, 8, 1)),
    ], emb)
    report = evaluate(conn, emb, [
        {"question": "프로젝트A 킥오프 언제?", "expect_source_id": "kick.md"},
        {"question": "존재하지 않는 주제 XYZQW", "expect_source_id": "none.md"},
    ])
    assert report["total"] == 2
    assert report["hit_at_3"] == 1
    assert report["rate"] == 0.5
    assert report["misses"][0]["question"].startswith("존재하지")
