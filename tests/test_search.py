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


def test_per_doc_cap_keeps_top_rrf_chunk_not_first_by_id(tmp_path: Path):
    # 문서 하나에 청크가 12개 있고, 마지막 청크에만 질의어가 들어 있다.
    # 문서당 청크 상한(3)을 적용할 때 chunk id 오름차순으로 자르면
    # 정작 질의와 가장 잘 맞는(마지막) 청크가 상한 밖으로 밀려나
    # 발췌 중심(centering)도 엉뚱한 앞부분 청크를 기준으로 잡히게 된다.
    conn = db.open_db(tmp_path / "s5.db")
    paragraphs = []
    for i in range(12):
        filler = f"섹션{i} 프로젝트B 내용 " + "나" * 750
        if i == 11:
            filler += " 고유마커777"
        paragraphs.append(filler)
    long_text = "\n\n".join(paragraphs)
    indexer.index_documents(
        conn, [Document("notes", "cap.md", "긴 문서", long_text, "/n/cap.md", datetime(2026, 8, 1))], EMB
    )
    hits = search.search(conn, EMB, "프로젝트B 고유마커777")
    assert hits and hits[0].source_id == "cap.md"
    assert "고유마커777" in hits[0].excerpt


def test_date_to_bare_date_includes_full_day(tmp_path: Path):
    conn = db.open_db(tmp_path / "s6.db")
    doc = Document(
        "notes", "afternoon.md", "오후 회의", "프로젝트C 오후 회의록 내용.",
        "/n/afternoon.md", datetime(2026, 8, 15, 14, 30),
    )
    indexer.index_documents(conn, [doc], EMB)

    included = search.search(conn, EMB, "프로젝트C", date_to="2026-08-15")
    assert any(h.source_id == "afternoon.md" for h in included)

    excluded = search.search(conn, EMB, "프로젝트C", date_to="2026-08-14")
    assert all(h.source_id != "afternoon.md" for h in excluded)
