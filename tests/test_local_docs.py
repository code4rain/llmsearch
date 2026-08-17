from pathlib import Path

import pytest
from llmsearch.connectors import local_docs
from llmsearch.summarize import FakeSummarizer


@pytest.fixture
def patch_extract(monkeypatch):
    def fake_extract(path: Path) -> str:
        if "drm" in path.name:
            raise RuntimeError("cannot open encrypted file")
        return f"{path.stem} 본문. 프로젝트A 관련 내용 " * 10
    monkeypatch.setattr(local_docs, "extract_text", fake_extract)


def run(tmp_path, docs_dir, state=None, prior=None):
    return local_docs.sync_local_docs(
        folders=[docs_dir], excludes=[], overrides=[],
        summarizer=FakeSummarizer(), summaries_dir=tmp_path / "summaries",
        projects=["프로젝트A"], areas=[], glossary="", class_rules="",
        state=state or {}, prior_map=prior or {},
    )


def test_summarize_classify_copy(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "킥오프.pptx").write_bytes(b"fake-pptx")
    result = run(tmp_path, docs)
    assert len(result.documents) == 1
    d = result.documents[0]
    assert d.extra["para_path"] == "Projects/프로젝트A"
    summary = Path(d.extra["summary_path"])
    assert summary.exists() and summary.suffix == ".md"
    assert (summary.parent / "킥오프.pptx").exists()  # 원본 복사 (스펙 §7.1)
    assert "## 요약" in d.text


def test_drm_fallback(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "drm_실적보고.pptx").write_bytes(b"encrypted")
    result = run(tmp_path, docs)
    d = result.documents[0]
    assert d.content_indexed is False
    assert "실적보고" in d.text  # 파일명 기반 설명


def test_category_move_no_duplicate(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    f = docs / "킥오프.pptx"; f.write_bytes(b"v1")
    r1 = run(tmp_path, docs)
    old_summary = Path(r1.documents[0].extra["summary_path"])
    # 재요약 시 분류가 바뀌는 상황을 prior_map 없이 강제: prior를 다른 카테고리로 주면 유지되므로,
    # 여기서는 prior가 Resources였다가 이번에 Projects로 가는 케이스 대신
    # 같은 파일을 수정해 prior=Projects 유지 + 파일 갱신 → 이동 없음/중복 없음 확인
    import os, time
    os.utime(f, (time.time() + 5, time.time() + 5))
    r2 = run(tmp_path, docs, state=r1.state,
             prior={r1.documents[0].source_id: ("Projects/프로젝트A", str(old_summary))})
    assert Path(r2.documents[0].extra["summary_path"]).exists()
    # summaries 아래에 같은 원본 복사본이 1개만 존재
    copies = list((tmp_path / "summaries").rglob("킥오프.pptx"))
    assert len(copies) == 1


def test_deleted_file_cleans_copies(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    f = docs / "킥오프.pptx"; f.write_bytes(b"v1")
    r1 = run(tmp_path, docs)
    sid = r1.documents[0].source_id
    summary = Path(r1.documents[0].extra["summary_path"])
    f.unlink()
    r2 = run(tmp_path, docs, state=r1.state, prior={sid: ("Projects/프로젝트A", str(summary))})
    assert r2.deleted_ids == [sid]
    assert not summary.exists()  # 요약본·복사본 정리 (스펙 §6 삭제 전파)


def test_filename_collision_disambiguated(tmp_path: Path, patch_extract):
    """두 폴더의 서로 다른 원본이 같은 파일명·같은 카테고리로 갈 때 덮어쓰기 없이 구분되어야 한다."""
    docs1 = tmp_path / "docs1"; docs1.mkdir()
    docs2 = tmp_path / "docs2"; docs2.mkdir()
    (docs1 / "보고서.pptx").write_bytes(b"v1-content-A")
    (docs2 / "보고서.pptx").write_bytes(b"v2-content-B-different-length")
    result = local_docs.sync_local_docs(
        folders=[docs1, docs2], excludes=[], overrides=[],
        summarizer=FakeSummarizer(), summaries_dir=tmp_path / "summaries",
        projects=["프로젝트A"], areas=[], glossary="", class_rules="",
        state={}, prior_map={},
    )
    assert len(result.documents) == 2
    summary_paths = {Path(d.extra["summary_path"]) for d in result.documents}
    assert len(summary_paths) == 2  # 서로 다른 summary_path
    for sp in summary_paths:
        assert sp.exists()
    copies = list((tmp_path / "summaries").rglob("*.pptx"))
    assert len(copies) == 2  # 원본 복사본도 2개, 서로 덮어쓰지 않음


def test_category_move_cleans_orphan_copy(tmp_path: Path, patch_extract):
    """카테고리 이동 시 이전 요약본이 이미 없어도(고아 상태) 이전 복사본은 정리돼야 한다."""
    docs = tmp_path / "docs"; docs.mkdir()
    f = docs / "이동문서.pptx"; f.write_bytes(b"v1")
    old_dir = tmp_path / "summaries" / "Areas" / "옛분류"
    old_dir.mkdir(parents=True)
    old_copy = old_dir / "이동문서.pptx"
    old_copy.write_bytes(b"stale-copy")
    old_summary_path = old_dir / "이동문서.pptx.md"  # 요약본은 이미 삭제된 상태를 시뮬레이션 (미생성)
    sid = str(f.resolve())
    prior = {sid: ("Areas/옛분류", str(old_summary_path))}
    overrides = [{"match": "path:*이동문서*", "target": "Projects/프로젝트A"}]
    result = local_docs.sync_local_docs(
        folders=[docs], excludes=[], overrides=overrides,
        summarizer=FakeSummarizer(), summaries_dir=tmp_path / "summaries",
        projects=["프로젝트A"], areas=[], glossary="", class_rules="",
        state={}, prior_map=prior,
    )
    assert result.documents[0].extra["para_path"] == "Projects/프로젝트A"
    assert not old_copy.exists()  # 요약본이 이미 없어도 고아 복사본은 정리돼야 함


def test_extract_text_real_smoke(tmp_path: Path):
    """markitdown 실변환 스모크 — 지원 포맷 파일이 없으면 skip."""
    pytest.importorskip("markitdown")
    # 텍스트 파일은 EXTENSIONS 밖이므로 변환기 직접 호출만 확인
    f = tmp_path / "t.txt"
    f.write_text("스모크 텍스트", encoding="utf-8")
    from llmsearch.connectors.local_docs import extract_text
    assert "스모크" in extract_text(f)
