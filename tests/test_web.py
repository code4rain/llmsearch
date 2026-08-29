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


def test_injected_slide_renderer_reaches_local_docs(tmp_path, monkeypatch):
    """create_app에 주입한 렌더러가 local_docs 동기화까지 전달된다."""
    from llmsearch.config import Config
    from llmsearch.connectors import local_docs
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.render import FakeSlideRenderer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app, run_sync

    docs = tmp_path / "watch"
    docs.mkdir()
    (docs / "deck.pptx").write_bytes(b"fake")
    # 72자 — garbled 임계(50) 초과·비전 임계(200) 미만: 증강 후에도 정상 인덱싱 경로 유지
    monkeypatch.setattr(local_docs, "extract_text", lambda p: "표지 제목 텍스트 " * 8)
    renderer = FakeSlideRenderer(images={"deck.pptx": [b"p1"]})
    cfg = Config(data_dir=tmp_path / "data", watch_folders=[docs])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), slide_renderer=renderer, enable_scheduler=False)
    entry = run_sync(app.state.llmsearch, "local_docs")
    assert entry["ok"] is True and entry["indexed"] == 1
    assert renderer.calls  # 주입된 렌더러가 실제 사용됨
    row = app.state.llmsearch["read_conn"].execute(
        "SELECT content_indexed FROM documents").fetchone()
    assert row[0] == 1  # 비전 증강 텍스트가 정상 인덱싱됨 (DRM 폴백 아님)


def test_slide_renderer_lazy_is_none_off_windows(tmp_path):
    """비Windows에서 지연 생성은 None — 비전 보완이 조용히 생략된다."""
    import os

    from llmsearch.web.app import _get_slide_renderer

    if hasattr(os, "startfile"):  # Windows에서는 이 테스트를 건너뜀 (COM 생성 방지)
        import pytest
        pytest.skip("non-Windows 전용 테스트")
    state = {}
    assert _get_slide_renderer(state) is None
    assert _get_slide_renderer(state) is None  # 캐시 후에도 동일


def test_archive_api_moves_project(tmp_path):
    from fastapi.testclient import TestClient

    from llmsearch.config import Config
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app

    cfg = Config(data_dir=tmp_path / "data")
    proj = cfg.summaries_dir / "Projects" / "알파"
    proj.mkdir(parents=True)
    (proj / "요약.md").write_text("# x", encoding="utf-8")
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    # TrustedHostMiddleware가 기본 Host("testserver")를 거부하므로 base_url 필수
    client = TestClient(app, base_url="http://127.0.0.1")

    listed = client.get("/api/para/projects").json()
    assert listed == [{"name": "알파", "doc_count": 0}]

    r = client.post("/api/archive", json={"project": "알파"})
    assert r.status_code == 200
    assert "config.yaml" in r.json()["hint"]
    assert (cfg.summaries_dir / "Archives" / "알파" / "요약.md").exists()
    assert client.get("/api/para/projects").json() == []


def test_archive_api_unknown_project_404(tmp_path):
    from fastapi.testclient import TestClient

    from llmsearch.config import Config
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app

    app = create_app(Config(data_dir=tmp_path / "data"), embedder=FakeEmbeddings(),
                     summarizer=FakeSummarizer(), answerer=FakeAnswerer(), enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")  # TrustedHost 통과
    assert client.post("/api/archive", json={"project": "없음"}).status_code == 404
    assert client.post("/api/archive", json={"project": ".."}).status_code == 400


def test_sync_paused_at_daily_limit_but_chat_still_works(tmp_path):
    """스펙 §10: 상한 도달 시 요약·인덱싱만 일시정지, 검색·답변은 유지."""
    from fastapi.testclient import TestClient

    from llmsearch.config import Config
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app, run_sync

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# 메모\n프로젝트A 내용", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], daily_api_call_limit=1)
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    state = app.state.llmsearch

    entry1 = run_sync(state, "notes")  # 인덱싱 1회 → embed 1건 기록 → 상한(1) 도달
    assert entry1["ok"] is True and entry1["indexed"] == 1

    entry2 = run_sync(state, "notes")  # 이제 게이트에 걸림
    assert entry2["ok"] is False and entry2["indexed"] == 0
    assert "일일 API 호출 상한" in entry2["error"] and "검색" in entry2["error"]
    assert state["log"][0]["error"] == entry2["error"]  # 로그 탭에 노출

    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post("/api/chat", json={"question": "프로젝트A 뭐였지?", "history": []})
    assert r.status_code == 200
    assert "event: done" in r.text  # 상한 도달 후에도 채팅 스트림 정상 완료


def test_chat_records_answer_usage(tmp_path):
    from fastapi.testclient import TestClient

    from llmsearch.config import Config
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app

    app = create_app(Config(data_dir=tmp_path / "data"), embedder=FakeEmbeddings(),
                     summarizer=FakeSummarizer(), answerer=FakeAnswerer(),
                     enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.post("/api/chat", json={"question": "q", "history": []})
    tracker = app.state.llmsearch["usage"]
    assert tracker.today_by_kind().get("answer", 0) >= 1


def test_run_sync_without_db_returns_error_entry(tmp_path: Path):
    """M6a 선행 리팩터: conn이 None(스키마 불일치 등)이면 예외 대신 error entry — 스케줄러 보호."""
    from llmsearch.web.app import run_sync

    client = make_app(tmp_path)
    state = client.app.state.llmsearch
    state["conn"] = None
    state["schema_mismatch"] = "index.db schema v0 != v1"
    entry = run_sync(state, "notes")
    assert entry["ok"] is False and "schema" in entry["error"]
    assert state["log"][0] is entry


def test_db_endpoints_503_without_read_conn(tmp_path: Path):
    client = make_app(tmp_path)
    state = client.app.state.llmsearch
    state["read_conn"] = None
    state["schema_mismatch"] = "index.db schema v0 != v1"
    assert client.post("/api/chat", json={"question": "q", "history": []}).status_code == 503
    assert client.get("/api/para/projects").status_code == 503
    assert client.post("/api/open", json={"url_or_path": "x"}).status_code == 503
    r = client.get("/api/sources")
    assert r.status_code == 200
    assert all(s["doc_count"] == 0 for s in r.json())
    assert r.json()[0]["schema_mismatch"] == "index.db schema v0 != v1"


def test_read_conn_is_looked_up_at_call_time(tmp_path: Path):
    """커넥션을 클로저가 아니라 state에서 조회해야 M6b가 재구축 후 교체할 수 있다."""
    from llmsearch import db

    client = make_app(tmp_path)
    state = client.app.state.llmsearch
    client.post("/api/sync/notes")
    fresh = db.open_db(state["config"].db_path)
    state["read_conn"].close()
    state["read_conn"] = fresh
    r = client.get("/api/sources")
    assert next(s for s in r.json() if s["source"] == "notes")["doc_count"] == 1


def test_mutating_endpoints_reject_foreign_origin(tmp_path: Path):
    """스펙 M6 §2: 임의 웹페이지의 CSRF(no-cors POST)로 동기화·아카이브·등록이 트리거되면 안 된다."""
    client = make_app(tmp_path)
    evil = {"Origin": "http://evil.example"}
    assert client.post("/api/sync/notes", headers=evil).status_code == 403
    assert client.post("/api/archive", json={"project": "x"}, headers=evil).status_code == 403
    assert client.post("/api/atlassian/register", json={"url": "x"}, headers=evil).status_code == 403
    assert client.request("DELETE", "/api/atlassian/registrations", json={"url": "x"}, headers=evil).status_code == 403
    assert client.post("/api/sync/notes", headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/sync/notes", headers={"Referer": "https://evil.example/page"}).status_code == 403


def test_mutating_endpoints_accept_local_origin_or_no_origin(tmp_path: Path):
    client = make_app(tmp_path)
    assert client.post("/api/sync/notes").status_code == 200  # curl/CLI — Origin 없음
    assert client.post("/api/sync/notes", headers={"Origin": "http://127.0.0.1:8642"}).status_code == 200
    assert client.post("/api/sync/notes", headers={"Origin": "http://localhost:8642"}).status_code == 200
    assert client.post("/api/sync/notes", headers={"Referer": "http://127.0.0.1:8642/"}).status_code == 200
