import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest
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


def test_rules_md_indexed_as_notes(tmp_path: Path):
    """스펙 §9: rules.md는 notes로 취급되어 인덱싱된다 — '내가 정한 규칙'도 검색 가능."""
    client = make_app(tmp_path)
    client.put("/api/rules", json={"text": "# 규칙 (rules.md)\n\n## 용어집\nPJA = 프로젝트A\n"})
    assert client.post("/api/sync/notes").json()["indexed"] == 2  # kick.md + rules.md
    read_conn = client.app.state.llmsearch["read_conn"]
    titles = {r[0] for r in read_conn.execute("SELECT title FROM documents WHERE source_type='notes'")}
    assert "규칙 (rules.md)" in titles


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
    assert client.post("/api/open", json={"url_or_path": "x"}, headers=evil).status_code == 403
    assert client.post("/api/chat", json={"question": "q", "history": []}, headers=evil).status_code == 403
    # urlsplit()이 ValueError를 던지는 기형 Origin(잘못된 IPv6 literal) — fail-closed로 403
    assert client.post("/api/sync/notes", headers={"Origin": "http://["}).status_code == 403


def test_mutating_endpoints_accept_local_origin_or_no_origin(tmp_path: Path):
    client = make_app(tmp_path)
    assert client.post("/api/sync/notes").status_code == 200  # curl/CLI — Origin 없음
    assert client.post("/api/sync/notes", headers={"Origin": "http://127.0.0.1:8642"}).status_code == 200
    assert client.post("/api/sync/notes", headers={"Origin": "http://localhost:8642"}).status_code == 200
    assert client.post("/api/sync/notes", headers={"Referer": "http://127.0.0.1:8642/"}).status_code == 200
    assert client.post("/api/chat", json={"question": "오리진 검사 통과 질의", "history": []},
                       headers={"Origin": "http://127.0.0.1:8642"}).status_code == 200


def test_rules_get_template_when_missing(tmp_path: Path):
    client = make_app(tmp_path)
    r = client.get("/api/rules")
    assert r.status_code == 200
    body = r.json()
    assert body["text"].startswith("# 규칙 (rules.md)")
    assert body["sections"] == ["용어집", "분류 규칙", "요약 규칙", "답변 규칙"]
    assert body["path"].endswith("rules.md")


def test_rules_put_saves_and_updates_answerer(tmp_path: Path):
    client = make_app(tmp_path)
    text = "# 규칙 (rules.md)\n\n## 용어집\nPJA = 프로젝트A\n\n## 답변 규칙\n두괄식\n"
    r = client.put("/api/rules", json={"text": text})
    assert r.status_code == 200 and r.json() == {"ok": True, "sections": ["용어집", "답변 규칙"]}
    path = client.app.state.llmsearch["config"].rules_md_path
    assert path.read_text(encoding="utf-8") == text
    assert not path.with_name(path.name + ".tmp").exists()  # 원자적 저장 — tmp 잔재 없음
    assert client.app.state.llmsearch["answerer"].rules["답변 규칙"] == "두괄식"
    assert client.get("/api/rules").json()["text"] == text


def test_rules_put_rejects_bad_input(tmp_path: Path):
    client = make_app(tmp_path)
    assert client.put("/api/rules", json={"text": 123}).status_code == 400
    assert client.put("/api/rules", json={}).status_code == 400
    big = "가" * (90 * 1024)  # UTF-8 3바이트 × 90K = 270KB > 256KB
    assert client.put("/api/rules", json={"text": big}).status_code == 400
    assert not client.app.state.llmsearch["config"].rules_md_path.exists()  # 파일 미변경
    assert client.put("/api/rules", json={"text": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_settings_tab_in_index(tmp_path: Path):
    client = make_app(tmp_path)
    html = client.get("/").text
    assert 'id="settings"' in html and 'id="rulesText"' in html and "설정" in html


def make_app_with_docs(tmp_path: Path, monkeypatch) -> TestClient:
    """local_docs 감시 폴더 1개(pptx 스텁) — markitdown 대신 짧은 본문 스텁."""
    from llmsearch.connectors import local_docs

    monkeypatch.setattr(local_docs, "extract_text", lambda p: f"{p.stem} 본문. 프로젝트A 관련 내용 " * 10)
    watch = tmp_path / "watch"; watch.mkdir()
    (watch / "설계.pptx").write_bytes(b"x")
    (watch / "회의록.pptx").write_bytes(b"y")
    cfg = Config(data_dir=tmp_path / "data", watch_folders=[watch], projects=["프로젝트A"])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1")


def test_resummarize_one_overwrites_summary_without_duplicates(tmp_path: Path, monkeypatch):
    """스펙 M6 §4: 센티널 치환 → prior_map 유지 → 기존 요약 md 덮어쓰기(중복본 없음)."""
    from llmsearch import indexer

    client = make_app_with_docs(tmp_path, monkeypatch)
    assert client.post("/api/sync/local_docs").json()["indexed"] == 2
    state = client.app.state.llmsearch
    sid = str((tmp_path / "watch" / "설계.pptx").resolve())
    before_map = indexer.get_para_map(state["read_conn"], sid)
    summary_before = state["usage"].today_by_kind()["summary"]

    r = client.post("/api/resummarize", json={"source_id": sid})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["indexed"] == 1 and body["reset"] == 1
    assert state["usage"].today_by_kind()["summary"] == summary_before + 1
    assert indexer.get_para_map(state["read_conn"], sid) == before_map  # 같은 요약 md 경로에 덮어씀
    md_files = list((tmp_path / "data" / "summaries").rglob("설계*.md"))
    assert len(md_files) == 1, md_files  # 해시 접미사 중복본이 생기지 않는다
    files = indexer.get_sync_state(state["read_conn"], "local_docs")["files"]
    assert files[sid] != [0.0, 0]  # 재요약 후 실제 시그니처로 복귀


def test_resummarize_all_and_count(tmp_path: Path, monkeypatch):
    client = make_app_with_docs(tmp_path, monkeypatch)
    client.post("/api/sync/local_docs")
    assert client.get("/api/resummarize/count").json() == {"count": 2}
    state = client.app.state.llmsearch
    summary_before = state["usage"].today_by_kind()["summary"]
    body = client.post("/api/resummarize", json={"all": True}).json()
    assert body["reset"] == 2 and body["indexed"] == 2
    assert state["usage"].today_by_kind()["summary"] == summary_before + 2


def test_resummarize_unknown_and_foreign_origin(tmp_path: Path, monkeypatch):
    client = make_app_with_docs(tmp_path, monkeypatch)
    client.post("/api/sync/local_docs")
    assert client.post("/api/resummarize", json={"source_id": "/없음.pptx"}).status_code == 404
    assert client.post("/api/resummarize", json={}).status_code == 404
    assert client.post("/api/resummarize", json={"all": True},
                       headers={"Origin": "http://evil.example"}).status_code == 403


def test_resummarize_deleted_file_is_detected(tmp_path: Path, monkeypatch):
    """센티널 치환은 sid를 prev에 남기므로 그 사이 삭제된 파일의 deleted 판정이 살아 있다."""
    client = make_app_with_docs(tmp_path, monkeypatch)
    client.post("/api/sync/local_docs")
    sid = str((tmp_path / "watch" / "설계.pptx").resolve())
    (tmp_path / "watch" / "설계.pptx").unlink()
    body = client.post("/api/resummarize", json={"source_id": sid}).json()
    assert body["indexed"] == 0 and body["deleted"] == 1
    assert client.get("/api/resummarize/count").json() == {"count": 1}


def test_resummarize_rejects_concurrent_run(tmp_path: Path, monkeypatch):
    """스펙 M6 §4·§8: 진행 중이면 409 — 더블클릭으로 요약 비용이 2배가 되지 않게."""
    client = make_app_with_docs(tmp_path, monkeypatch)
    client.post("/api/sync/local_docs")
    state = client.app.state.llmsearch
    state["resummarize_lock"].acquire()  # 진행 중 상황 재현
    try:
        assert client.post("/api/resummarize", json={"all": True}).status_code == 409
    finally:
        state["resummarize_lock"].release()


def test_resummarize_respects_daily_limit_gate(tmp_path: Path, monkeypatch):
    """상한 도달 시 센티널을 쓰기 전에 409로 거부 — 다음 스케줄러 라운드의 N-콜 버스트를 막는다."""
    from llmsearch import indexer

    client = make_app_with_docs(tmp_path, monkeypatch)
    client.post("/api/sync/local_docs")
    state = client.app.state.llmsearch
    before_files = dict(indexer.get_sync_state(state["read_conn"], "local_docs").get("files", {}))
    state["usage"].daily_limit = 1  # 이미 초과 상태
    r = client.post("/api/resummarize", json={"all": True})
    assert r.status_code == 409
    assert "일일 API 호출 상한" in r.json()["detail"]
    after_files = indexer.get_sync_state(state["read_conn"], "local_docs").get("files", {})
    assert after_files == before_files  # 센티널([0.0, 0])로 치환되지 않음 — 상태 미변경


def test_usage_endpoint_and_line(tmp_path: Path):
    from datetime import date

    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    client.post("/api/chat", json={"question": "사용량 표시 확인용 질의", "history": []})  # 스위트 내 유일한 질문 — 질의 임베딩 캐시 회피
    u = client.get("/api/usage").json()
    assert u["today"]["embed"] >= 2 and u["today"]["answer"] == 1
    assert u["limit"] == 0 and u["indexing_allowed"] is True
    assert u["days"][-1]["date"] == date.today().isoformat()
    assert u["days"][-1]["total"] == u["total"]
    assert 'id="usageLine"' in client.get("/").text


def test_banner_and_rebuild_button_in_index(tmp_path: Path):
    client = make_app(tmp_path)
    html = client.get("/").text
    assert 'id="banner"' in html and 'id="rebuildBtn"' in html and "loadStatus()" in html


def make_app_mixed(tmp_path: Path, monkeypatch) -> TestClient:
    """notes 1 + local_docs 1 — 소스 필터가 실제로 결과를 가르는지 보기 위한 구성."""
    from llmsearch.connectors import local_docs

    monkeypatch.setattr(local_docs, "extract_text", lambda p: "프로젝트A 아키텍처 설계 본문 " * 10)
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "kick.md").write_text("# 프로젝트A 킥오프\n프로젝트A 일정 확정", encoding="utf-8")
    watch = tmp_path / "watch"; watch.mkdir()
    (watch / "설계.pptx").write_bytes(b"x")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], watch_folders=[watch], projects=["프로젝트A"])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.post("/api/sync/notes"); client.post("/api/sync/local_docs")
    return client


def _sources_of(body: str) -> list[str]:
    line = next(l for l in body.splitlines() if l.startswith("data: ["))  # text/error 이벤트는 data: "..."
    return [h["source_type"] for h in json.loads(line[len("data: "):])]


def test_chat_source_filter_forces_presearch(tmp_path: Path, monkeypatch):
    client = make_app_mixed(tmp_path, monkeypatch)
    with client.stream("POST", "/api/chat", json={"question": "프로젝트A 필터 검증 질의", "history": [],
                                                   "filters": {"source_filter": ["notes"]}}) as r:
        body = "".join(r.iter_text())
    assert _sources_of(body) == ["notes"]
    with client.stream("POST", "/api/chat", json={"question": "프로젝트A 필터 없음 질의", "history": []}) as r:
        body = "".join(r.iter_text())
    assert set(_sources_of(body)) == {"notes", "local_docs"}
    assert client.app.state.llmsearch["answerer"].last_filters_note == ""


def test_chat_filters_note_delivered_and_normalized(tmp_path: Path, monkeypatch):
    client = make_app_mixed(tmp_path, monkeypatch)
    client.post("/api/chat", json={"question": "고지 전달 질의", "history": [],
                                   "filters": {"source_filter": ["local_docs", "notes", "notes"],
                                               "date_from": "2026-08-01", "date_to": "", "sender": ""}})
    note = client.app.state.llmsearch["answerer"].last_filters_note
    assert note.startswith("(사용자 필터 적용: 소스=notes,local_docs, 기간=2026-08-01~")  # SOURCES 순서·중복 제거
    assert "빈 배열" in note


def test_chat_filter_validation_400_and_no_answer_count(tmp_path: Path, monkeypatch):
    client = make_app_mixed(tmp_path, monkeypatch)
    before = client.app.state.llmsearch["usage"].today_by_kind().get("answer", 0)
    bad = [
        {"source_filter": "notes"}, {"source_filter": ["slack"]}, {"source_filter": [1]},
        {"date_from": "2026-13-45"}, {"date_to": 20260801}, {"date_from": "20260801"},
        {"date_from": "2026-W01-1"}, {"sender": "x" * 201},
        {"sender": "kim@corp.com", "source_filter": ["notes"]},
    ]
    for f in bad:
        r = client.post("/api/chat", json={"question": "q", "history": [], "filters": f})
        assert r.status_code == 400, f
    assert client.post("/api/chat", json={"question": "q", "history": [], "filters": "x"}).status_code == 400
    assert client.app.state.llmsearch["usage"].today_by_kind().get("answer", 0) == before  # 400은 answer 미계상
    ok = client.post("/api/chat", json={"question": "sender 단독 허용 질의", "history": [],
                                        "filters": {"sender": " kim@corp.com ", "source_filter": []}})
    assert ok.status_code == 200
    assert "발신자=kim@corp.com" in client.app.state.llmsearch["answerer"].last_filters_note


def test_apply_filters_fills_only_missing_args():
    from llmsearch.web.app import _apply_filters

    calls = []

    def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
        calls.append((source_filter, date_from, date_to, sender))
        return []

    f = {"source_filter": ["notes"], "date_from": "2026-08-01", "date_to": None, "sender": "a@b"}
    wrapped = _apply_filters(search_fn, f)
    wrapped("q")                                                   # 선검색: 전부 필터
    wrapped("q", source_filter=["jira"], date_from="", date_to="2026-09-01", sender=None)  # 툴: 명시값 우선, falsy 채움
    assert calls == [(["notes"], "2026-08-01", None, "a@b"), (["jira"], "2026-08-01", "2026-09-01", "a@b")]
    assert _apply_filters(search_fn, {"source_filter": None, "date_from": None, "date_to": None, "sender": None}) is search_fn


def test_chat_filter_ui_in_index(tmp_path: Path):
    client = make_app(tmp_path)
    html = client.get("/").text
    for needle in ('id="filters"', 'id="fDateFrom"', 'id="fDateTo"', 'id="fSender"', "filters-note",
                   "입력 시 메일만 검색됩니다", "resp.ok"):
        assert needle in html, needle


# 스위트 내 유일한 질문 — 질의 임베딩 캐시 회피 (test_golden.py가 같은 문장을 먼저 임베딩하면 embed 카운트가 어긋난다)
GOLDEN_TWO = ("- question: 프로젝트A 킥오프 웹평가 전용 질의\n  expect_source_id: kick.md\n"
              "- question: 존재하지 않는 주제 WEBONLYXYZQW\n  expect_source_id: none.md\n")


def test_golden_get_template_and_put(tmp_path: Path):
    client = make_app(tmp_path)
    g = client.get("/api/eval/golden").json()
    assert g["count"] == 0 and g["text"].startswith("#") and g["path"].endswith("golden.yaml")
    assert client.put("/api/eval/golden", json={"text": g["text"]}).json() == {"ok": True, "count": 0}  # 템플릿 저장 OK
    r = client.put("/api/eval/golden", json={"text": GOLDEN_TWO})
    assert r.status_code == 200 and r.json()["count"] == 2
    path = client.app.state.llmsearch["config"].data_dir / "golden.yaml"
    assert path.read_text(encoding="utf-8") == GOLDEN_TWO and not path.with_name("golden.yaml.tmp").exists()
    assert client.get("/api/eval/golden").json()["count"] == 2
    assert client.put("/api/eval/golden", json={"text": "question: q\n"}).status_code == 400
    assert client.put("/api/eval/golden", json={"text": 5}).status_code == 400
    assert client.put("/api/eval/golden", json={"text": "- question: [x\n"}).status_code == 400
    assert client.put("/api/eval/golden", json={"text": GOLDEN_TWO},
                      headers={"Origin": "http://evil.example"}).status_code == 403
    assert path.read_text(encoding="utf-8") == GOLDEN_TWO  # 실패한 PUT은 파일 미변경


def test_golden_run_reports_rank_and_pass(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    assert client.post("/api/eval/golden/run", json={}).status_code == 400  # 파일 없음
    client.put("/api/eval/golden", json={"text": GOLDEN_TWO})
    r = client.post("/api/eval/golden/run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and body["hit_at_3"] == 1 and body["rate"] == 0.5
    assert body["target"] == 0.7 and body["pass"] is False
    assert body["cases"][0]["rank"] == 1 and body["cases"][1]["rank"] is None
    assert client.app.state.llmsearch["usage"].today_by_kind()["embed"] >= 3  # notes 1 + 질의 2


def test_golden_run_refusals(tmp_path: Path):
    client = make_app(tmp_path)
    client.put("/api/eval/golden", json={"text": GOLDEN_TWO})
    state = client.app.state.llmsearch
    state["rebuilding"] = True
    assert client.post("/api/eval/golden/run", json={}).status_code == 409
    state["rebuilding"] = False
    state["evaluate_lock"].acquire()
    try:
        assert client.post("/api/eval/golden/run", json={}).status_code == 409
    finally:
        state["evaluate_lock"].release()
    assert client.post("/api/eval/golden/run", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    state["read_conn"] = None
    assert client.post("/api/eval/golden/run", json={}).status_code == 503


def test_golden_run_rebuilding_leaves_evaluating_flag_and_lock_clean(tmp_path: Path):
    """rebuilding 중 409를 받아도 evaluating 플래그·락이 남지 않아야 한다 (rebuild.claim과의 경합 창 회귀 방지)."""
    client = make_app(tmp_path)
    client.put("/api/eval/golden", json={"text": GOLDEN_TWO})
    state = client.app.state.llmsearch
    state["rebuilding"] = True
    r = client.post("/api/eval/golden/run", json={})
    assert r.status_code == 409
    state["rebuilding"] = False
    assert state["evaluating"] is False
    assert state["evaluate_lock"].acquire(blocking=False)
    state["evaluate_lock"].release()


def test_golden_run_embedding_failure_hides_message(tmp_path: Path, monkeypatch):
    from llmsearch import search as search_mod

    client = make_app(tmp_path)
    client.put("/api/eval/golden", json={"text": GOLDEN_TWO})

    def boom(*a, **k):
        raise RuntimeError("secret api key sk-123")
    monkeypatch.setattr(search_mod, "search", boom)
    r = client.post("/api/eval/golden/run", json={})
    assert r.status_code == 502 and "RuntimeError" in r.json()["detail"] and "sk-123" not in r.text


def test_golden_ui_in_index(tmp_path: Path):
    client = make_app(tmp_path)
    html = client.get("/").text
    for needle in ('id="goldenText"', 'id="runGoldenBtn"', 'id="goldenTable"', "loadGolden()"):
        assert needle in html, needle


def _sse_events(body: str) -> list[str]:
    return [l[len("event: "):] for l in body.splitlines() if l.startswith("event: ")]


def test_chats_crud(tmp_path: Path):
    client = make_app(tmp_path)
    assert client.get("/api/chats").json() == []
    r = client.post("/api/chats", json={})
    assert r.status_code == 200 and r.json()["title"] == "새 대화"
    a = r.json()["id"]
    b = client.post("/api/chats", json={"title": "  둘째   세션 "}).json()["id"]
    assert [s["id"] for s in client.get("/api/chats").json()] == [b, a]
    assert client.get("/api/chats").json()[0]["title"] == "둘째 세션"
    assert client.post("/api/chats", json={"title": 5}).status_code == 400
    assert client.post("/api/chats", json={"title": "x" * 201}).status_code == 400
    assert client.get(f"/api/chats/{a}").json()["messages"] == []
    assert client.get("/api/chats/999").status_code == 404
    assert client.get("/api/chats/abc").status_code == 422
    assert client.delete(f"/api/chats/{a}").json() == {"ok": True}
    assert client.delete(f"/api/chats/{a}").status_code == 404
    evil = {"Origin": "http://evil.example"}
    assert client.post("/api/chats", json={}, headers=evil).status_code == 403
    assert client.delete(f"/api/chats/{b}", headers=evil).status_code == 403


def test_chats_503_when_store_unavailable_but_chat_works(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    state = client.app.state.llmsearch
    state["chat_store"] = None
    state["chat_store_error"] = "OperationalError"
    r = client.get("/api/chats")
    assert r.status_code == 503 and "OperationalError" in r.json()["detail"]
    assert client.post("/api/chats", json={}).status_code == 503
    assert client.post("/api/chat", json={"question": "저장소 없음 질의", "history": [], "session_id": 1}).status_code == 503
    r = client.post("/api/chat", json={"question": "저장소 없음 무세션 질의", "history": []})
    assert r.status_code == 200 and "event: done" in r.text and "event: saved" not in r.text


def test_chat_with_session_saves_and_uses_server_history(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    state = client.app.state.llmsearch
    sid = client.post("/api/chats", json={}).json()["id"]
    r = client.post("/api/chat", json={"question": "세션 첫 질문 킥오프", "session_id": sid,
                                       "history": [{"role": "user", "content": "무시되어야 함"}],
                                       "filters": {"source_filter": ["notes"]}})
    assert r.status_code == 200
    assert _sse_events(r.text)[-2:] == ["saved", "done"]
    assert state["answerer"].last_history == []  # 첫 질문: 서버 이력 비어 있음, 페이로드 history 무시
    s = client.get(f"/api/chats/{sid}").json()
    assert s["title"] == "세션 첫 질문 킥오프"  # "새 대화" → 첫 질문
    assert [m["role"] for m in s["messages"]] == ["user", "assistant"]
    assert s["messages"][0]["filters"]["source_filter"] == ["notes"]
    assert s["messages"][1]["sources"] and s["messages"][1]["sources"][0]["source_type"] == "notes"
    assert "excerpt" in s["messages"][1]["sources"][0] and s["messages"][1]["content"].startswith("[1]")
    client.post("/api/chat", json={"question": "세션 둘째 질문", "session_id": sid})
    assert [m["content"] for m in state["answerer"].last_history] == ["세션 첫 질문 킥오프", s["messages"][1]["content"]]
    assert client.get(f"/api/chats/{sid}").json()["title"] == "세션 첫 질문 킥오프"  # 제목은 첫 질문 유지


def test_chat_without_session_is_stateless(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    hist = [{"role": "user", "content": "이전"}, {"role": "assistant", "content": "답"}]
    r = client.post("/api/chat", json={"question": "무세션 질의", "history": hist})
    assert r.status_code == 200 and "event: saved" not in r.text
    assert client.app.state.llmsearch["answerer"].last_history == hist
    assert client.get("/api/chats").json() == []


def test_chat_bad_session_id_404_and_no_answer_count(tmp_path: Path):
    client = make_app(tmp_path)
    before = client.app.state.llmsearch["usage"].today_by_kind().get("answer", 0)
    for bad in (True, 999, "1", 1.5, 10**30, -(10**30)):  # 10**30: sqlite INTEGER(64비트) 범위 밖
        assert client.post("/api/chat", json={"question": "q", "session_id": bad}).status_code == 404, bad
    sid = client.post("/api/chats", json={}).json()["id"]
    assert client.post("/api/chat", json={"question": "  ", "session_id": sid}).status_code == 400  # 빈 질문
    assert client.post("/api/chat", json={"question": {"a": 1}, "session_id": sid}).status_code == 400  # 비문자열
    assert client.post("/api/chat", json={"question": [1], "session_id": sid}).status_code == 400  # 비문자열
    assert client.app.state.llmsearch["usage"].today_by_kind().get("answer", 0) == before


def test_chats_get_delete_out_of_range_session_id_404(tmp_path: Path):
    client = make_app(tmp_path)
    huge = "1000000000000000000000"  # sqlite INTEGER(64비트) 범위 밖 — OverflowError 경로
    assert client.get(f"/api/chats/{huge}").status_code == 404
    assert client.delete(f"/api/chats/{huge}").status_code == 404


class _ExplodingAnswerer(FakeAnswerer):
    """스트림 도중 예외 — finally 저장 경로 검증. (클라이언트 중단은 test_chat_client_abort_saves_partial이
    aclose()로 검증; 이 클래스는 답변기 예외 경로.)"""

    def __init__(self, after: int):
        super().__init__()
        self.after = after  # 이 개수의 text 이벤트 뒤에 폭발 (0이면 첫 토큰 전)

    def answer_stream(self, question, history, search_fn, filters_note: str = ""):
        self.last_history = list(history)
        for i in range(self.after):
            yield {"type": "text", "text": f"부분{i} "}
        raise RuntimeError("stream broke")


class _SlowAnswerer(FakeAnswerer):
    """text 이벤트를 여러 번 나눠 내보내 클라이언트 중단(aclose) 타이밍을 재현한다."""

    def answer_stream(self, question, history, search_fn, filters_note: str = ""):
        self.last_history = list(history)
        for i in range(5):
            yield {"type": "text", "text": f"부분{i} "}
        yield {"type": "sources", "hits": search_fn(question)}


def _app_with_answerer(tmp_path: Path, answerer) -> TestClient:
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "kick.md").write_text("# 킥오프\n내용", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(), answerer=answerer, enable_scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)


def test_chat_partial_answer_saved_in_finally(tmp_path: Path):
    # raise_server_exceptions=False에서 답변기 예외는 TestClient까지 전파되지 않고(빈 본문으로 종료),
    # finally가 부분 답변을 저장한다 — try/except 불필요, 저장 결과로 검증한다.
    client = _app_with_answerer(tmp_path, _ExplodingAnswerer(after=2))
    sid = client.post("/api/chats", json={}).json()["id"]
    with client.stream("POST", "/api/chat", json={"question": "중단 질의", "session_id": sid}) as r:
        body = "".join(r.iter_text())
    assert "event: saved" not in body
    msgs = client.get(f"/api/chats/{sid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "부분0 부분1 "  # 부분 답변 보존


def test_chat_empty_answer_saves_placeholder(tmp_path: Path):
    client = _app_with_answerer(tmp_path, _ExplodingAnswerer(after=0))
    sid = client.post("/api/chats", json={}).json()["id"]
    with client.stream("POST", "/api/chat", json={"question": "즉시 중단 질의", "session_id": sid}) as r:
        "".join(r.iter_text())
    msgs = client.get(f"/api/chats/{sid}").json()["messages"]
    assert msgs[1]["content"] == "(답변 없음 — 응답 전 중단)"  # 빈 text 블록 방지


def test_chat_client_abort_saves_partial(tmp_path: Path):
    """진짜 GeneratorExit 재현 — 라우트 함수를 직접 호출해 StreamingResponse의 비동기 이터레이터를
    aclose()로 닫는다. finally 안에 yield가 있으면 여기서 RuntimeError가 난다 (없음을 검증)."""
    client = _app_with_answerer(tmp_path, _SlowAnswerer())
    sid = client.post("/api/chats", json={}).json()["id"]
    fn = next(r.endpoint for r in client.app.routes if getattr(r, "path", "") == "/api/chat")
    resp = fn({"question": "중단 질의", "session_id": sid})

    async def drive():
        it = resp.body_iterator
        await it.__anext__(); await it.__anext__()
        await it.aclose()  # finally 안에 yield가 있으면 여기서 RuntimeError
    asyncio.run(drive())
    msgs = client.get(f"/api/chats/{sid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "부분0 부분1 "


def test_shutdown_closes_chat_store(tmp_path: Path):
    notes = tmp_path / "notes"; notes.mkdir()
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(), answerer=FakeAnswerer(),
                     enable_scheduler=False)
    state = app.state.llmsearch
    with TestClient(app, base_url="http://127.0.0.1"):
        pass  # 컨텍스트 종료 시 shutdown 이벤트 발화 — chat_store.close() 호출
    with pytest.raises(sqlite3.ProgrammingError):
        state["chat_store"].list_sessions()


def test_export_slug_rules():
    from llmsearch.web.app import _export_slug

    assert _export_slug("프로젝트A 킥오프") == "프로젝트A_킥오프"
    assert _export_slug("../../etc/passwd") == "etc_passwd"
    assert "/" not in _export_slug("a/b\\c:d*e?f") and ".." not in _export_slug("..")
    assert _export_slug("") == "chat" and _export_slug("   ") == "chat"
    assert len(_export_slug("가" * 100)) <= 40
    # 리뷰 발견: strip("_")은 _sanitize_segment가 Windows 예약 디바이스명 이스케이프용으로
    # 붙인 선행 "_"까지 지워 원래 예약명("CON")으로 되돌린다 — rstrip만 써야 한다.
    assert _export_slug("CON") == "_CON"
    assert _export_slug("nul") == "_nul"
    assert _export_slug("_x_") == "_x"


def test_chat_export_replaces_stale_filename_on_title_change(tmp_path: Path):
    """리뷰 발견: 기본 제목("새 대화")으로 먼저 내보낸 뒤 첫 질문으로 제목이 자동 변경되면
    파일명 slug도 바뀐다 — 이전 이름 파일이 orphan으로 남지 않고 정리되어야 세션당 파일 1개가
    유지된다."""
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    sid = client.post("/api/chats", json={}).json()["id"]  # 기본 제목
    path_a = Path(client.post(f"/api/chats/{sid}/export", json={}).json()["path"])
    assert path_a.name == f"chat-{sid}-새_대화.md"
    client.post("/api/chat", json={"question": "제목 변경 유발 질의", "session_id": sid})
    path_b = Path(client.post(f"/api/chats/{sid}/export", json={}).json()["path"])
    assert path_b != path_a
    exports = client.app.state.llmsearch["config"].exports_dir
    assert not path_a.exists()
    assert sorted(p.name for p in exports.iterdir()) == [path_b.name]
    assert "## Q1." in path_b.read_text(encoding="utf-8")


def test_chat_export_write_failure_preserves_existing_file(tmp_path: Path, monkeypatch):
    """리뷰 발견: 이전 이름 파일 정리를 새 파일 쓰기보다 먼저 하면, 쓰기(os.replace)가 실패할 때
    직전까지 유효했던 export가 사라져 세션의 export가 0개가 된다 — 정리는 쓰기 성공 후에만
    해야 크래시-세이프하다."""
    import llmsearch.web.app as app_module

    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    sid = client.post("/api/chats", json={}).json()["id"]
    path_a = Path(client.post(f"/api/chats/{sid}/export", json={}).json()["path"])
    client.post("/api/chat", json={"question": "쓰기 실패 재현 질의", "session_id": sid})
    exports = client.app.state.llmsearch["config"].exports_dir

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(app_module.os, "replace", boom)
    r = client.post(f"/api/chats/{sid}/export", json={})
    assert r.status_code == 500
    assert path_a.exists()  # 실패한 새 파일 쓰기 때문에 기존 export가 지워지면 안 된다
    assert sorted(p.name for p in exports.iterdir()) == [path_a.name]

    monkeypatch.undo()
    r = client.post(f"/api/chats/{sid}/export", json={})
    assert r.status_code == 200
    path_b = Path(r.json()["path"])
    assert path_b != path_a
    assert not path_a.exists()
    assert sorted(p.name for p in exports.iterdir()) == [path_b.name]


def test_chat_export_deterministic_and_overwrites(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    sid = client.post("/api/chats", json={"title": "내보내기 세션/테스트"}).json()["id"]
    client.post("/api/chat", json={"question": "내보내기 첫 질의", "session_id": sid})
    r = client.post(f"/api/chats/{sid}/export", json={})
    assert r.status_code == 200, r.text
    path = Path(r.json()["path"])
    exports = client.app.state.llmsearch["config"].exports_dir
    assert path.parent == exports and path.name == f"chat-{sid}-내보내기_세션_테스트.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# [대화기록] 내보내기 세션/테스트\n> 이 문서는") and "## Q1. 내보내기 첫 질의" in text
    client.post("/api/chat", json={"question": "내보내기 둘째 질의", "session_id": sid})
    assert Path(client.post(f"/api/chats/{sid}/export", json={}).json()["path"]) == path
    assert "## Q2. 내보내기 둘째 질의" in path.read_text(encoding="utf-8")
    assert sorted(p.name for p in exports.iterdir()) == [path.name]  # 재내보내기는 같은 파일, tmp 잔재 없음
    assert client.post("/api/chats/999/export", json={}).status_code == 404
    assert client.post("/api/chats/1000000000000000000000/export", json={}).status_code == 404
    assert client.post(f"/api/chats/{sid}/export", json={}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_exports_indexed_as_notes_when_enabled(tmp_path: Path):
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("# 메모", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], export_to_notes=True)
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(), answerer=FakeAnswerer(), enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.post("/api/sync/notes")
    sid = client.post("/api/chats", json={}).json()["id"]
    client.post("/api/chat", json={"question": "노트 인덱싱 질의", "session_id": sid})
    client.post(f"/api/chats/{sid}/export", json={})
    assert client.post("/api/sync/notes").json()["indexed"] == 1
    titles = {r[0] for r in app.state.llmsearch["read_conn"].execute("SELECT title FROM documents WHERE source_type='notes'")}
    assert any(t.startswith("[대화기록] ") for t in titles)
    cfg2 = Config(data_dir=tmp_path / "data2", notes_folders=[notes])  # 기본 false → exports 미포함
    app2 = create_app(cfg2, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(), answerer=FakeAnswerer(), enable_scheduler=False)
    (cfg2.exports_dir).mkdir(parents=True)
    (cfg2.exports_dir / "chat-1-x.md").write_text("# [대화기록] x", encoding="utf-8")
    assert TestClient(app2, base_url="http://127.0.0.1").post("/api/sync/notes").json()["indexed"] == 1  # a.md만
