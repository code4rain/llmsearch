from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llmsearch import db, indexer, rebuild
from llmsearch.config import Config
from llmsearch.connectors import local_docs
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.llm import FakeAnswerer
from llmsearch.outlook.client import FakeOutlookClient
from llmsearch.summarize import FakeSummarizer
from llmsearch.web.app import create_app


def make_state(tmp_path: Path, monkeypatch, daily_limit: int = 0):
    """notes 1 + local_docs 2(pptx 스텁) 구성의 앱 state. TestClient는 필요한 테스트만 만든다."""
    monkeypatch.setattr(local_docs, "extract_text", lambda p: f"{p.stem} 본문. 프로젝트A 관련 내용 " * 10)
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "kick.md").write_text("# 프로젝트A 킥오프\n8월 1일", encoding="utf-8")
    watch = tmp_path / "watch"; watch.mkdir()
    (watch / "설계.pptx").write_bytes(b"x")
    (watch / "회의록.pptx").write_bytes(b"y")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], watch_folders=[watch],
                 projects=["프로젝트A"], daily_api_call_limit=daily_limit)
    # Outlook은 빈 Fake 주입 — 재수집 대상에 outlook_*가 포함되므로 COM 지연 import 경로를 타지 않게
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), outlook_client=FakeOutlookClient(mails={}, appointments=[]),
                     enable_scheduler=False)
    return app, app.state.llmsearch


def doc_count(conn, source=None) -> int:
    if source:
        return conn.execute("SELECT COUNT(*) FROM documents WHERE source_type=?", (source,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]


def test_marker_roundtrip(tmp_path: Path, monkeypatch):
    _, state = make_state(tmp_path, monkeypatch)
    conn = state["conn"]
    assert rebuild.marker_present(conn) is False
    rebuild.set_marker(conn); conn.commit()
    assert rebuild.marker_present(conn) is True
    other = db.open_db(state["config"].db_path)
    assert rebuild.marker_present(other) is True  # 커밋됨 — 다른 커넥션에서 보임
    other.close()
    rebuild.clear_marker(conn); conn.commit()
    assert rebuild.marker_present(conn) is False


def test_precheck_refusals(tmp_path: Path, monkeypatch):
    _, state = make_state(tmp_path, monkeypatch, daily_limit=1)
    state["usage"].record("embed", 5)
    with pytest.raises(rebuild.RebuildRefused, match="상한"):
        rebuild.precheck(state)
    state["usage"].daily_limit = 0
    state["config"].watch_folders.append(tmp_path / "unmounted")
    with pytest.raises(rebuild.RebuildRefused) as ei:
        rebuild.precheck(state)
    assert ei.value.missing_folders == [str(tmp_path / "unmounted")]
    rebuild.precheck(state, force=True)  # force면 폴더 경고 무시
    state["config"].watch_folders.pop()
    state["rebuilding"] = True
    with pytest.raises(rebuild.RebuildRefused, match="진행 중"):
        rebuild.precheck(state)
    state["rebuilding"] = False
    state["resummarizing"] = True
    with pytest.raises(rebuild.RebuildRefused, match="진행 중"):
        rebuild.precheck(state)


def test_reset_index_keeps_para_map_and_local_state(tmp_path: Path, monkeypatch):
    from llmsearch.web.app import run_sync

    _, state = make_state(tmp_path, monkeypatch)
    run_sync(state, "notes"); run_sync(state, "local_docs")
    conn = state["conn"]
    assert doc_count(conn) == 3
    local_state = indexer.get_sync_state(conn, "local_docs")
    sid = next(iter(local_state["files"]))
    para_before = indexer.get_para_map(conn, sid)
    summary_md = Path(para_before[1])
    assert summary_md.exists()

    info = rebuild.reset_index(state)
    assert info == {"documents_deleted": 3}
    assert doc_count(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    assert indexer.get_sync_state(conn, "notes") == {}                 # 다른 소스 상태 삭제
    assert indexer.get_sync_state(conn, "local_docs") == local_state   # local_docs 상태 보존
    assert indexer.get_para_map(conn, sid) == para_before              # para_map 보존
    assert summary_md.exists()                                          # 요약 md 보존
    assert rebuild.marker_present(conn) is True
    assert state["force_reindex_local_docs"] is True
    assert indexer.delete_all_documents(conn) == 0
