import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from llmsearch.config import Config
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.llm import FakeAnswerer
from llmsearch.summarize import FakeSummarizer
from llmsearch.web.app import create_app


def make_app(tmp_path: Path) -> TestClient:
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "kick.md").write_text("# 프로젝트A 킥오프\n8월 1일 진행", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], projects=["프로젝트A"])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    # TrustedHostMiddleware가 127.0.0.1/localhost만 허용하므로 TestClient 기본 host인
    # "testserver" 대신 허용된 호스트를 base_url로 지정한다.
    return TestClient(app, base_url="http://127.0.0.1")


def test_index_page(tmp_path: Path):
    client = make_app(tmp_path)
    r = client.get("/")
    assert r.status_code == 200 and "llmsearch" in r.text


def test_manual_sync_and_sources(tmp_path: Path):
    client = make_app(tmp_path)
    r = client.post("/api/sync/notes")
    assert r.status_code == 200
    assert r.json()["indexed"] == 1
    r = client.get("/api/sources")
    notes_status = next(s for s in r.json() if s["source"] == "notes")
    assert notes_status["doc_count"] == 1
    assert notes_status["last_sync"] is not None


def test_chat_sse(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    with client.stream("POST", "/api/chat", json={"question": "킥오프 언제?", "history": []}) as r:
        body = "".join(r.iter_text())
    assert "event: sources" in body
    assert "event: done" in body
    assert "킥오프" in body


def test_sync_unknown_source(tmp_path: Path):
    client = make_app(tmp_path)
    assert client.post("/api/sync/outlook").status_code == 404


def test_sync_log(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    log = client.get("/api/log").json()
    assert log and log[0]["source"] == "notes" and log[0]["ok"] is True


def test_sync_failure_rolls_back_connection(tmp_path: Path, monkeypatch):
    """run_sync 실패 시 conn.rollback()이 호출돼 미완료 트랜잭션이 남지 않아야 한다.

    sqlite3.Connection은 C 확장 타입이라 rollback을 직접 monkeypatch할 수 없으므로,
    실패 후 conn.in_transaction이 False임을 확인해 미완료 트랜잭션이 정리됐는지 검증한다
    (수정 전에는 index_documents 중 예외가 나면 최종 commit()에 도달하지 못해 열린
    트랜잭션이 그대로 남는다).
    """
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "kick.md").write_text("# 프로젝트A 킥오프\n8월 1일 진행", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], projects=["프로젝트A"])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")
    state = app.state.llmsearch

    def boom(texts):
        raise RuntimeError("embedder boom")

    monkeypatch.setattr(state["embedder"], "embed", boom)

    r = client.post("/api/sync/notes")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert state["conn"].in_transaction is False


def test_read_conn_separate_from_write_conn(tmp_path: Path):
    """읽기(chat/sources)는 쓰기(run_sync)와 별도의 커넥션을 사용해야 한다."""
    client = make_app(tmp_path)
    state = client.app.state.llmsearch
    assert state["read_conn"] is not state["conn"]

    client.post("/api/sync/notes")
    r = client.get("/api/sources")
    notes_status = next(s for s in r.json() if s["source"] == "notes")
    assert notes_status["doc_count"] == 1  # read_conn을 통해서도 커밋된 쓰기가 조회됨


def test_concurrent_manual_sync_is_serialized(tmp_path: Path):
    client = make_app(tmp_path)
    results = [None, None]

    def call(i):
        results[i] = client.post("/api/sync/notes")

    threads = [threading.Thread(target=call, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 200 for r in results)
    log = client.get("/api/log").json()
    assert len(log) == 2
    assert all(e["source"] == "notes" and e["ok"] is True for e in log)
    r = client.get("/api/sources")
    notes_status = next(s for s in r.json() if s["source"] == "notes")
    assert notes_status["doc_count"] == 1
