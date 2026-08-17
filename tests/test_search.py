from datetime import datetime, timedelta
from pathlib import Path

from llmsearch import db, indexer, search
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.models import Document

EMB = FakeEmbeddings(dim=768)


def setup_index(tmp_path: Path):
    conn = db.open_db(tmp_path / "s.db")
    now = datetime(2026, 8, 15)
    docs = [
        Document("notes", "kickoff.md", "프로젝트A 킥오프", "프로젝트A 킥오프 회의록. 일정과 담당자 결정.",
                 "/n/kickoff.md", now, extra={"para_path": "Projects/프로젝트A"}),
        Document("notes", "lunch.md", "점심 기록", "오늘 점심은 김치찌개.", "/n/lunch.md", now),
        Document("notes", "old.md", "프로젝트A 과거 자료", "프로젝트A 초기 기획 메모.",
                 "/n/old.md", now - timedelta(days=700), extra={"para_path": "Archives/프로젝트A"}),
        Document("local_docs", "spec.pptx", "프로젝트A 발표자료", "프로젝트A 발표자료 요약. 로드맵 포함.",
                 "/d/spec.pptx", now),
    ]
    indexer.index_documents(conn, docs, EMB)
    # documents.para_path 반영 (인덱서는 extra로 받아 컬럼에 기록)
    return conn


def test_hybrid_search_finds_relevant(tmp_path: Path):
    conn = setup_index(tmp_path)
    hits = search.search(conn, EMB, "프로젝트A 킥오프 회의록")
    assert hits, "결과 없음"
    assert hits[0].source_id == "kickoff.md"
    ids = [h.source_id for h in hits]
    assert "lunch.md" not in ids[:2]


def test_source_filter(tmp_path: Path):
    conn = setup_index(tmp_path)
    hits = search.search(conn, EMB, "프로젝트A", source_filter=["local_docs"])
    assert hits and all(h.source_type == "local_docs" for h in hits)


def test_archive_decay(tmp_path: Path):
    conn = setup_index(tmp_path)
    hits = search.search(conn, EMB, "프로젝트A 기획")
    ids = [h.source_id for h in hits]
    # Archive 문서는 감쇠되어 활성 문서보다 아래 (제외는 아님)
    assert "old.md" in ids
    assert ids.index("old.md") > ids.index("kickoff.md")


def test_excerpt_capped(tmp_path: Path):
    conn = db.open_db(tmp_path / "s2.db")
    long_text = "\n\n".join(f"섹션{i} 프로젝트B 내용 " + "가" * 500 for i in range(30))
    indexer.index_documents(
        conn, [Document("notes", "big.md", "긴 문서", long_text, "/n/big.md", datetime(2026, 8, 1))], EMB
    )
    hits = search.search(conn, EMB, "프로젝트B 섹션5")
    assert hits and len(hits[0].excerpt) <= 6000
