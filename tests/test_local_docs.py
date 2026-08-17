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


def test_collision_identity_drift_no_orphan(tmp_path: Path, patch_extract):
    """해시로 구분됐던 요약본이 평문 이름으로 되돌아갈 때 이전 해시본이 고아로 남지 않아야 한다.

    3라운드 재현 (리뷰어 지적 시나리오):
    1) 동명 파일 두 개 동기화 → A는 평문, B는 해시 이름
    2) A 원본 삭제 → A의 평문 요약본/복사본 정리(평문 슬롯 해제), B는 변경 없어 그대로 해시 유지
    3) B의 mtime 갱신 후 재동기화 → 평문 슬롯이 비어 있으므로 B가 평문 이름으로 회귀 →
       이전 해시 요약본·복사본은 고아로 남지 않고 정리되어야 함
    """
    docs1 = tmp_path / "docs1"; docs1.mkdir()
    docs2 = tmp_path / "docs2"; docs2.mkdir()
    fa = docs1 / "보고서.pptx"; fa.write_bytes(b"v1-content-A")
    fb = docs2 / "보고서.pptx"; fb.write_bytes(b"v2-content-B-different-length")

    def sync(state, prior):
        return local_docs.sync_local_docs(
            folders=[docs1, docs2], excludes=[], overrides=[],
            summarizer=FakeSummarizer(), summaries_dir=tmp_path / "summaries",
            projects=["프로젝트A"], areas=[], glossary="", class_rules="",
            state=state, prior_map=prior,
        )

    sid_a = str(fa.resolve()); sid_b = str(fb.resolve())

    # Round 1: A는 평문 이름, B는 충돌로 해시 이름
    r1 = sync({}, {})
    by_sid = {d.source_id: d for d in r1.documents}
    summary_a = Path(by_sid[sid_a].extra["summary_path"])
    summary_b = Path(by_sid[sid_b].extra["summary_path"])
    assert summary_a.name == "보고서.pptx.md"
    assert summary_b.name != "보고서.pptx.md"
    hashed_summary_b = summary_b
    hashed_copy_b = hashed_summary_b.parent / hashed_summary_b.name.removesuffix(".md")
    assert hashed_copy_b.exists()

    # Round 2: A 원본 삭제 → 평문 슬롯 해제. B는 변경 없어 재처리 안 됨(해시 이름 유지)
    fa.unlink()
    prior_r2 = {
        sid_a: ("Projects/프로젝트A", str(summary_a)),
        sid_b: ("Projects/프로젝트A", str(summary_b)),
    }
    r2 = sync(r1.state, prior_r2)
    assert sid_a in r2.deleted_ids
    assert not summary_a.exists()
    assert hashed_summary_b.exists()
    assert hashed_copy_b.exists()

    # Round 3: B의 mtime 갱신 → 재처리 강제 → 평문 이름으로 회귀해야 함
    import os, time
    os.utime(fb, (time.time() + 10, time.time() + 10))
    prior_r3 = {sid_b: ("Projects/프로젝트A", str(hashed_summary_b))}
    r3 = sync(r2.state, prior_r3)
    new_summary_b = Path(r3.documents[0].extra["summary_path"])
    assert new_summary_b.name == "보고서.pptx.md"  # 평문 이름으로 회귀
    # 이전 해시 요약본·복사본은 고아로 남으면 안 됨
    assert not hashed_summary_b.exists()
    assert not hashed_copy_b.exists()
    # summaries 아래에는 요약본 1개, 복사본 1개만 남아야 함
    all_md = list((tmp_path / "summaries").rglob("보고서*.md"))
    all_copies = list((tmp_path / "summaries").rglob("보고서*.pptx"))
    assert len(all_md) == 1
    assert len(all_copies) == 1


def test_one_file_failure_does_not_abort_sync(tmp_path: Path, patch_extract):
    """요약기가 특정 파일에서 예외를 던져도 다른 파일은 정상 동기화되고 예외가 새지 않는다."""

    class FlakySummarizer(FakeSummarizer):
        def summarize_and_classify(self, title, text, *a, **kw):
            if "boom" in title:
                raise RuntimeError("summarizer exploded")
            return super().summarize_and_classify(title, text, *a, **kw)

    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "boom.pptx").write_bytes(b"fake-pptx")
    (docs / "ok.pptx").write_bytes(b"fake-pptx")

    result = local_docs.sync_local_docs(
        folders=[docs], excludes=[], overrides=[],
        summarizer=FlakySummarizer(), summaries_dir=tmp_path / "summaries",
        projects=["프로젝트A"], areas=[], glossary="", class_rules="",
        state={}, prior_map={},
    )
    titles = [d.title for d in result.documents]
    assert "ok.pptx" in titles
    assert "boom.pptx" not in titles
    ok_sid = str((docs / "ok.pptx").resolve())
    assert ok_sid in result.state["files"]
    # 실패한 파일은 seen에 재시도 센티널로 남는다: deleted로 오판되지 않으면서도
    # 실제 (mtime, size)와는 달라 다음 동기화에서 강제로 재시도된다.
    boom_sid = str((docs / "boom.pptx").resolve())
    boom_path = docs / "boom.pptx"
    real_sig = [boom_path.stat().st_mtime, boom_path.stat().st_size]
    assert boom_sid in result.state["files"]
    assert result.state["files"][boom_sid] != real_sig


def test_transient_failure_does_not_delete_previously_synced_file(tmp_path: Path, patch_extract):
    """이전에 성공 동기화된 파일이 이번 실행에서 일시적으로 실패해도 삭제로 오판돼
    기존 요약본/복사본이 지워지면 안 되고, 다음 성공 실행에서 재동기화돼야 한다."""
    docs = tmp_path / "docs"; docs.mkdir()
    f = docs / "flaky.pptx"; f.write_bytes(b"v1")

    # Round 1: 정상 동기화 성공
    r1 = run(tmp_path, docs)
    sid = r1.documents[0].source_id
    assert r1.documents[0].extra["para_path"] == "Projects/프로젝트A"
    summary_path = Path(r1.documents[0].extra["summary_path"])
    copy_path = summary_path.parent / "flaky.pptx"
    assert summary_path.exists() and copy_path.exists()

    # Round 2: 파일이 변경돼 재처리 대상이 되지만, 요약기가 일시적으로 예외를 던짐
    import os, time
    os.utime(f, (time.time() + 5, time.time() + 5))

    class FlakyOnceSummarizer(FakeSummarizer):
        def summarize_and_classify(self, title, text, *a, **kw):
            raise RuntimeError("transient failure")

    prior = {sid: ("Projects/프로젝트A", str(summary_path))}
    r2 = local_docs.sync_local_docs(
        folders=[docs], excludes=[], overrides=[],
        summarizer=FlakyOnceSummarizer(), summaries_dir=tmp_path / "summaries",
        projects=["프로젝트A"], areas=[], glossary="", class_rules="",
        state=r1.state, prior_map=prior,
    )
    assert r2.documents == []             # 이번 라운드엔 재동기화되지 않음
    assert sid not in r2.deleted_ids      # 삭제로 오판되지 않음
    assert summary_path.exists() and copy_path.exists()  # 기존 요약본/복사본 그대로 유지

    # Round 3: 요약기가 정상으로 돌아오면 다음 동기화에서 자동으로 재시도돼 재동기화된다.
    r3 = run(tmp_path, docs, state=r2.state, prior=prior)
    assert len(r3.documents) == 1
    assert r3.documents[0].source_id == sid


def test_extract_text_real_smoke(tmp_path: Path):
    """markitdown 실변환 스모크 — 지원 포맷 파일이 없으면 skip."""
    pytest.importorskip("markitdown")
    # 텍스트 파일은 EXTENSIONS 밖이므로 변환기 직접 호출만 확인
    f = tmp_path / "t.txt"
    f.write_text("스모크 텍스트", encoding="utf-8")
    from llmsearch.connectors.local_docs import extract_text
    assert "스모크" in extract_text(f)
