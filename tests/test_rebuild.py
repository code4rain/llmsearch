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


def test_precheck_and_rebuild_refused_while_evaluating(tmp_path: Path, monkeypatch):
    app, state = make_state(tmp_path, monkeypatch)
    client = client_of(app)
    state["evaluating"] = True
    try:
        assert client.post("/api/rebuild", json={}).status_code == 409
        with pytest.raises(rebuild.RebuildRefused, match="진행 중"):
            rebuild.claim(state)
    finally:
        state["evaluating"] = False


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


def test_reset_index_rolls_back_on_failure(tmp_path: Path, monkeypatch):
    """M6b 최종 리뷰 Important 1: delete_all_documents 실패는 부분 삭제를 커밋하지 않는다 —
    안 그러면 이후 무관한 sync가 그 부분 삭제 상태를 그대로 이어받는다."""
    from llmsearch.web.app import run_sync

    _, state = make_state(tmp_path, monkeypatch)
    run_sync(state, "notes"); run_sync(state, "local_docs")
    conn = state["conn"]
    before = doc_count(conn)
    assert before == 3

    def boom(_conn):
        raise RuntimeError("boom")

    monkeypatch.setattr(rebuild.indexer, "delete_all_documents", boom)

    with pytest.raises(RuntimeError):
        rebuild.reset_index(state)

    assert doc_count(conn) == before
    assert rebuild.marker_present(conn) is False
    assert conn.in_transaction is False
    assert state["force_reindex_local_docs"] is False


def client_of(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def wait_resync(state, timeout=30):
    t = state.get("rebuild_thread")
    assert t is not None
    t.join(timeout)
    assert not t.is_alive() and state["rebuilding"] is False


def test_rebuild_endpoint_restores_docs_without_llm(tmp_path: Path, monkeypatch):
    app, state = make_state(tmp_path, monkeypatch)
    client = client_of(app)
    client.post("/api/sync/notes"); client.post("/api/sync/local_docs")
    usage_before = dict(state["usage"].today_by_kind())
    conn = state["read_conn"]
    assert doc_count(conn) == 3
    sid = next(iter(indexer.get_sync_state(conn, "local_docs")["files"]))
    para_before = indexer.get_para_map(conn, sid)

    r = client.post("/api/rebuild", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["phase"] == "resync" and body["documents_deleted"] == 3
    assert body["targets"] == ["notes", "local_docs", "outlook_mail", "outlook_cal"]  # 등록 없는 confluence/jira 제외
    wait_resync(state)

    after = state["usage"].today_by_kind()
    assert after.get("summary", 0) == usage_before.get("summary", 0)  # 요약 md 재사용 — LLM 미호출
    assert after.get("vision", 0) == usage_before.get("vision", 0)
    assert after["embed"] > usage_before["embed"]
    assert doc_count(conn, "notes") == 1 and doc_count(conn, "local_docs") == 2
    assert indexer.get_para_map(conn, sid) == para_before
    assert rebuild.marker_present(conn) is False                       # local_docs 성공 후 마커 삭제
    assert state["force_reindex_local_docs"] is False
    assert client.get("/api/status").json() == {
        "schema_mismatch": None, "rebuild_in_progress": False, "rebuilding": False, "resummarizing": False,
        "evaluating": False, "windows": False}  # WSL 지원(§A6)으로 "windows" 필드 추가 — 의도된 변경
    log_sources = [e["source"] for e in state["log"][:4]]
    assert set(log_sources) >= {"notes", "local_docs"}


def test_rebuild_refusals_and_force(tmp_path: Path, monkeypatch):
    app, state = make_state(tmp_path, monkeypatch, daily_limit=1)
    client = client_of(app)
    client.post("/api/sync/notes")  # embed 1 → 상한 도달
    r = client.post("/api/rebuild", json={})
    assert r.status_code == 409 and "상한" in r.json()["detail"]
    assert doc_count(state["read_conn"]) == 1  # DB 무변경

    state["usage"].daily_limit = 0
    state["config"].watch_folders.append(tmp_path / "unmounted")
    r = client.post("/api/rebuild", json={})
    assert r.status_code == 409 and r.json()["missing_folders"] == [str(tmp_path / "unmounted")]
    r = client.post("/api/rebuild", json={"force": True})
    assert r.status_code == 200
    wait_resync(state)
    assert doc_count(state["read_conn"], "notes") == 1
    assert client.post("/api/rebuild", json={}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_resummarize_and_second_rebuild_refused_while_rebuilding(tmp_path: Path, monkeypatch):
    app, state = make_state(tmp_path, monkeypatch)
    client = client_of(app)
    client.post("/api/sync/local_docs")
    state["rebuilding"] = True  # 재수집 스레드가 도는 중이라고 가정
    try:
        assert client.post("/api/resummarize", json={"all": True}).status_code == 409
        assert client.post("/api/rebuild", json={}).status_code == 409
        assert client.post("/api/rebuild/resume", json={}).status_code == 409
    finally:
        state["rebuilding"] = False


def test_marker_survives_until_local_docs_succeeds(tmp_path: Path, monkeypatch):
    """마커는 local_docs run_sync가 플래그를 소비한 뒤에만 삭제 — 게이트에 막히면 유지."""
    from llmsearch.web.app import run_sync

    _, state = make_state(tmp_path, monkeypatch)
    run_sync(state, "local_docs")
    rebuild.reset_index(state)
    state["usage"].daily_limit = 1  # 이미 초과
    entry = run_sync(state, "local_docs")
    assert entry["ok"] is False
    assert rebuild.marker_present(state["conn"]) is True and state["force_reindex_local_docs"] is True
    state["usage"].daily_limit = 0
    entry = run_sync(state, "local_docs")
    assert entry["ok"] is True and entry["indexed"] == 2
    assert rebuild.marker_present(state["conn"]) is False and state["force_reindex_local_docs"] is False


def test_startup_detects_marker_and_resume(tmp_path: Path, monkeypatch):
    app1, state1 = make_state(tmp_path, monkeypatch)
    from llmsearch.web.app import run_sync
    run_sync(state1, "local_docs")
    rebuild.reset_index(state1)  # 재수집 전에 프로세스가 죽었다고 가정
    state1["conn"].close(); state1["read_conn"].close()

    cfg = state1["config"]
    app2 = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                      answerer=FakeAnswerer(), outlook_client=FakeOutlookClient(mails={}, appointments=[]),
                      enable_scheduler=False)
    state2 = app2.state.llmsearch
    assert state2["force_reindex_local_docs"] is True
    client = client_of(app2)
    assert client.get("/api/status").json()["rebuild_in_progress"] is True
    r = client.post("/api/rebuild/resume", json={})
    assert r.status_code == 200 and r.json()["phase"] == "resync"
    wait_resync(state2)
    assert doc_count(state2["read_conn"], "local_docs") == 2
    assert client.get("/api/status").json()["rebuild_in_progress"] is False
    assert client.post("/api/rebuild/resume", json={}).status_code == 409  # 재개할 것 없음


def test_resume_refused_when_daily_limit_exceeded(tmp_path: Path, monkeypatch):
    """M6b 최종 리뷰 minor: resume은 claim 전에 precheck(force=True)를 돌린다 — 안 그러면
    상한이 이미 초과된 상태에서 매 소스마다 게이트에 막혀 도는 스레드만 돌고 사유를 알 수 없다."""
    app1, state1 = make_state(tmp_path, monkeypatch, daily_limit=1)
    from llmsearch.web.app import run_sync
    run_sync(state1, "local_docs")
    rebuild.reset_index(state1)  # 마커를 남기고 재수집 전에 중단됐다고 가정
    state1["usage"].record("embed", 5)  # 다음 기동 전에 이미 상한 초과 (usage.json은 data_dir 공유로 영속)
    state1["conn"].close(); state1["read_conn"].close()

    cfg = state1["config"]
    app2 = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                      answerer=FakeAnswerer(), outlook_client=FakeOutlookClient(mails={}, appointments=[]),
                      enable_scheduler=False)
    state2 = app2.state.llmsearch
    client = client_of(app2)
    assert client.get("/api/status").json()["rebuild_in_progress"] is True

    r = client.post("/api/rebuild/resume", json={})
    assert r.status_code == 409
    assert "상한" in r.json()["detail"]
    assert r.json()["missing_folders"] == []
    assert state2["rebuilding"] is False  # precheck에서 거부 — claim 전이라 선점되지 않음


def test_schema_mismatch_boot_and_recover(tmp_path: Path, monkeypatch):
    app1, state1 = make_state(tmp_path, monkeypatch)
    from llmsearch.web.app import run_sync
    run_sync(state1, "notes"); run_sync(state1, "local_docs")
    usage_before = dict(state1["usage"].today_by_kind())
    sid = next(iter(indexer.get_sync_state(state1["conn"], "local_docs")["files"]))
    para_before = indexer.get_para_map(state1["conn"], sid)
    state1["conn"].execute("UPDATE meta SET value='0' WHERE key='schema_version'"); state1["conn"].commit()
    state1["conn"].close(); state1["read_conn"].close()

    cfg = state1["config"]
    app2 = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                      answerer=FakeAnswerer(), outlook_client=FakeOutlookClient(mails={}, appointments=[]),
                      enable_scheduler=False)  # 기동 성공
    state2 = app2.state.llmsearch
    assert state2["conn"] is None and "schema" in state2["schema_mismatch"]
    client = client_of(app2)
    assert client.post("/api/chat", json={"question": "스키마 불일치 질의", "history": []}).status_code == 503
    assert client.post("/api/sync/notes").status_code == 503
    s = client.get("/api/status").json()
    assert s["schema_mismatch"] and s["rebuild_in_progress"] is False
    assert client.get("/api/sources").json()[0]["schema_mismatch"]

    r = client.post("/api/rebuild", json={})
    assert r.status_code == 200 and r.json()["legacy_maps_recovered"] == 2
    backup_path = Path(r.json()["backup"])
    assert backup_path.exists()                                        # 손상 DB는 rename 백업 — unlink 아님
    wait_resync(state2)
    conn = state2["read_conn"]
    assert conn is not None and state2["schema_mismatch"] is None
    assert doc_count(conn, "local_docs") == 2 and doc_count(conn, "notes") == 1
    assert indexer.get_para_map(conn, sid) == para_before
    after = state2["usage"].today_by_kind()
    assert after.get("summary", 0) == usage_before.get("summary", 0)  # legacy 매핑 회수 → 요약 재사용
    assert client.post("/api/sync/notes").status_code == 200           # 가드 해제


def test_recover_schema_mismatch_restores_backup_on_failure(tmp_path: Path, monkeypatch):
    """M6b 최종 리뷰 Important 2: open_db 실패로 새 index.db를 못 열면 백업을 원위치로 되돌린다 —
    안 그러면 DB가 통째로 사라져 다음 시도가 legacy 매핑 없이 전량 재요약·중복 md를 만든다."""
    from llmsearch.web.app import run_sync

    app1, state1 = make_state(tmp_path, monkeypatch)
    run_sync(state1, "notes"); run_sync(state1, "local_docs")
    sid = next(iter(indexer.get_sync_state(state1["conn"], "local_docs")["files"]))
    state1["conn"].execute("UPDATE meta SET value='0' WHERE key='schema_version'"); state1["conn"].commit()
    state1["conn"].close(); state1["read_conn"].close()

    cfg = state1["config"]
    app2 = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                      answerer=FakeAnswerer(), outlook_client=FakeOutlookClient(mails={}, appointments=[]),
                      enable_scheduler=False)
    state2 = app2.state.llmsearch
    assert state2["conn"] is None and state2["schema_mismatch"]

    def boom(_path):
        raise RuntimeError("boom")

    monkeypatch.setattr(rebuild.db, "open_db", boom)

    with pytest.raises(RuntimeError):
        rebuild.recover_schema_mismatch(state2)

    assert cfg.db_path.exists()
    assert not list(cfg.db_path.parent.glob(cfg.db_path.name + ".corrupt-*"))
    assert state2["conn"] is None
    rows, _local_state = db.read_legacy_maps(cfg.db_path)
    assert any(r[0] == sid for r in rows)                              # legacy 매핑 여전히 회수 가능


def test_resummarize_refused_while_rebuilding_leaves_state_untouched(tmp_path: Path, monkeypatch):
    """M6b 리뷰 Important 2: rebuilding 중 재요약 요청은 sync_state를 건드리기 전에 거부돼야 한다
    (거부가 락 임계구역 밖에서 일어나면 claim()과의 TOCTOU 창에서 센티널이 먼저 기록될 수 있다)."""
    app, state = make_state(tmp_path, monkeypatch)
    client = client_of(app)
    client.post("/api/sync/local_docs")
    before = indexer.get_sync_state(state["read_conn"], "local_docs")
    state["rebuilding"] = True
    try:
        r = client.post("/api/resummarize", json={"all": True})
        assert r.status_code == 409
    finally:
        state["rebuilding"] = False
    after = indexer.get_sync_state(state["read_conn"], "local_docs")
    assert after == before             # 센티널이 기록되지 않음 — 락 첫 줄에서 거부됐다는 증거
    assert state["resummarizing"] is False


def test_recover_schema_mismatch_backfills_sentinel_for_orphaned_para_map(tmp_path: Path, monkeypatch):
    """M6b 리뷰 Important 4: local_docs sync_state가 유실되고 para_map만 남아도
    recover_schema_mismatch가 para_map의 모든 sid를 RETRY_SENTINEL로 채워 재요약을 강제하고,
    prior_category가 유지돼 해시 접미사 중복 md를 만들지 않는다(문서 수는 그대로 2건)."""
    app1, state1 = make_state(tmp_path, monkeypatch)
    from llmsearch.web.app import run_sync

    run_sync(state1, "local_docs")
    conn1 = state1["conn"]
    sids = sorted(r[0] for r in conn1.execute("SELECT source_id FROM para_map").fetchall())
    assert len(sids) == 2
    conn1.execute("DELETE FROM sync_state WHERE source_type='local_docs'")  # 상태만 유실 — para_map은 보존
    conn1.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    conn1.commit()
    state1["conn"].close(); state1["read_conn"].close()

    cfg = state1["config"]
    app2 = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                      answerer=FakeAnswerer(), outlook_client=FakeOutlookClient(mails={}, appointments=[]),
                      enable_scheduler=False)
    state2 = app2.state.llmsearch
    assert state2["conn"] is None and state2["schema_mismatch"]
    summary_before = state2["usage"].today_by_kind().get("summary", 0)  # usage.json은 cfg 공유로 state1 몫 포함

    info = rebuild.recover_schema_mismatch(state2)
    assert info["legacy_maps_recovered"] == 2
    files = indexer.get_sync_state(state2["conn"], "local_docs")["files"]
    assert set(files) == set(sids)
    for sid in sids:
        assert files[sid] == list(local_docs.RETRY_SENTINEL)  # 유실된 상태를 센티널로 백필

    entry = run_sync(state2, "local_docs")
    assert entry["ok"] is True
    assert doc_count(state2["read_conn"], "local_docs") == 2
    summary_after = state2["usage"].today_by_kind().get("summary", 0)
    assert summary_after == summary_before + 2  # 센티널 → md 재사용 아님, 전량 재요약(기대됨)

    for sid in sids:
        _para_path, summary_path = indexer.get_para_map(state2["read_conn"], sid)
        summary_dir = Path(summary_path).parent
        original_name = Path(sid).name
        matches = sorted(p.name for p in summary_dir.glob("*.md") if p.name.startswith(Path(original_name).stem))
        assert matches == [original_name + ".md"]  # 해시 접미사(__<8hex>) 중복본 없음 — sid당 정확히 하나


def test_run_cli_headless_rebuild(tmp_path: Path, monkeypatch):
    from llmsearch.web.app import _scheduled_sources, run_sync

    _, state = make_state(tmp_path, monkeypatch)
    run_sync(state, "notes"); run_sync(state, "local_docs")
    summary_before = state["usage"].today_by_kind().get("summary", 0)
    lines: list[str] = []
    code = rebuild.run_cli(state, run_sync, _scheduled_sources(state), yes=True, out=lines.append)
    assert code == 0
    assert doc_count(state["read_conn"]) == 3 and rebuild.marker_present(state["conn"]) is False
    assert state["usage"].today_by_kind().get("summary", 0) == summary_before
    assert any("documents 3" in ln or "3건" in ln for ln in lines) and any("local_docs" in ln for ln in lines)


def test_run_cli_prompt_and_refusal(tmp_path: Path, monkeypatch):
    from llmsearch.web.app import _scheduled_sources, run_sync

    _, state = make_state(tmp_path, monkeypatch)
    run_sync(state, "notes")
    lines: list[str] = []
    code = rebuild.run_cli(state, run_sync, _scheduled_sources(state), yes=False,
                           input_fn=lambda _p: "n", out=lines.append)
    assert code == 2 and doc_count(state["read_conn"]) == 1  # 취소 — 무변경
    state["usage"].daily_limit = 1; state["usage"].record("embed", 5)
    code = rebuild.run_cli(state, run_sync, _scheduled_sources(state), yes=True, out=lines.append)
    assert code == 2 and any("상한" in ln for ln in lines)


def test_run_cli_partial_failure_returns_1_and_server_still_boots(tmp_path: Path, monkeypatch):
    """M6b 리뷰 Important 2: 일부 소스 재수집 실패는 거부/취소(2)가 아니라 1 — 초기화는 성공했으니
    서버는 계속 기동해야 한다(__main__은 code==2일 때만 sys.exit)."""
    from llmsearch.web.app import run_sync

    for var in ("ATLASSIAN_PAT", "ATLASSIAN_USER", "ATLASSIAN_PASSWORD", "ATLASSIAN_COOKIE",
                "CONFLUENCE_PAT", "CONFLUENCE_USER", "CONFLUENCE_PASSWORD", "CONFLUENCE_COOKIE",
                "JIRA_PAT", "JIRA_USER", "JIRA_PASSWORD", "JIRA_COOKIE"):
        monkeypatch.delenv(var, raising=False)
    _, state = make_state(tmp_path, monkeypatch)
    lines: list[str] = []
    code = rebuild.run_cli(state, run_sync, ["notes", "confluence"], yes=True, out=lines.append)
    assert code == 1
    assert any("재수집 실패 소스: confluence" in ln for ln in lines)
    assert doc_count(state["read_conn"], "notes") == 1  # 성공한 소스는 정상 복구


def test_run_cli_precheck_refusal_skips_prompt(tmp_path: Path, monkeypatch):
    """M6b 리뷰 Important 1: precheck 거부는 확인 프롬프트보다 먼저 — input_fn이 호출되면 안 된다."""
    _, state = make_state(tmp_path, monkeypatch, daily_limit=1)
    state["usage"].record("embed", 5)

    def run_sync_unused(_state, _source):
        pytest.fail("run_sync should not be called")

    code = rebuild.run_cli(state, run_sync_unused, ["notes"], yes=False,
                           input_fn=lambda _p: pytest.fail("prompt shown"), out=lambda _s: None)
    assert code == 2


def test_main_parses_rebuild_flags(monkeypatch, tmp_path: Path):
    import llmsearch.__main__ as m

    calls = {}
    monkeypatch.setattr(m, "load_config", lambda p: Config(data_dir=tmp_path / "data"))
    monkeypatch.setattr(m, "create_app", lambda cfg: type("A", (), {"state": type("S", (), {"llmsearch": {"x": 1}})()})())
    monkeypatch.setattr(m, "load_dotenv", lambda *a, **k: None)  # 실 .env가 os.environ에 새지 않게
    monkeypatch.setattr(m, "run_cli", lambda state, run_sync, sources, yes, force: calls.update(yes=yes, force=force, state=state) or 0)
    monkeypatch.setattr(m, "_scheduled_sources", lambda state: ["notes"])
    monkeypatch.setattr(m.uvicorn, "run", lambda *a, **k: calls.update(served=True))
    monkeypatch.setattr("sys.argv", ["llmsearch", "--config", "c.yaml", "--rebuild", "--yes", "--force"])
    m.main()
    assert calls == {"yes": True, "force": True, "state": {"x": 1}, "served": True}


def test_rebuild_preserves_chats_db(tmp_path: Path, monkeypatch):
    app, state = make_state(tmp_path, monkeypatch)
    client = client_of(app)
    sid = client.post("/api/chats", json={"title": "보존 확인"}).json()["id"]
    client.post("/api/chat", json={"question": "재구축 보존 질의", "session_id": sid})
    assert client.post("/api/rebuild", json={}).status_code == 200
    wait_resync(state)
    s = client.get(f"/api/chats/{sid}").json()
    assert s["title"] == "보존 확인" and len(s["messages"]) == 2  # 명시 제목이므로 첫 질문으로 덮이지 않는다
    assert s["messages"][0]["content"] == "재구축 보존 질의"
