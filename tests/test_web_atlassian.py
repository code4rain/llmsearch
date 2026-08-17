import os
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from llmsearch.atlassian.registry import Registry
from llmsearch.web.app import SOURCES, _scheduled_sources

from llmsearch.atlassian.client import FakeAtlassianClient
from llmsearch.config import Config
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.llm import FakeAnswerer
from llmsearch.summarize import FakeSummarizer
from llmsearch.web.app import create_app


def make_client(tmp_path: Path, atlassian=None) -> TestClient:
    cfg = Config(data_dir=tmp_path / "data")
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), atlassian_client=atlassian,
                     enable_scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1")


def fake_atlassian():
    return FakeAtlassianClient(
        pages={"123": {"id": "123", "space": "ENG", "title": "설계", "html": "<p>내용</p>",
                       "version": 1, "updated": "2026-08-01T10:00:00", "ancestors": [],
                       "url": "https://wiki/pages/123"}},
        issues={"PROJ-1": {"key": "PROJ-1", "summary": "버그", "description": "설명",
                           "status": "Open", "assignee": "김철수",
                           "updated": "2026-08-02T09:00:00",
                           "url": "https://jira/browse/PROJ-1", "comments": []}},
    )


def test_register_and_list_and_remove(tmp_path: Path):
    client = make_client(tmp_path)
    r = client.post("/api/atlassian/register",
                    json={"url": "https://wiki/pages/viewpage.action?pageId=123"})
    assert r.status_code == 200 and r.json()["kind"] == "confluence_page"
    r = client.post("/api/atlassian/register", json={"url": "https://jira/browse/PROJ-1"})
    assert r.json()["kind"] == "jira_issue"
    regs = client.get("/api/atlassian/registrations").json()
    assert len(regs) == 2
    assert client.post("/api/atlassian/register", json={"url": "https://x.com/a"}).status_code == 400
    r = client.request("DELETE", "/api/atlassian/registrations",
                       json={"url": "https://jira/browse/PROJ-1"})
    assert r.status_code == 200
    assert len(client.get("/api/atlassian/registrations").json()) == 1


def test_confluence_and_jira_sync(tmp_path: Path):
    client = make_client(tmp_path, atlassian=fake_atlassian())
    client.post("/api/atlassian/register",
                json={"url": "https://wiki/pages/viewpage.action?pageId=123"})
    client.post("/api/atlassian/register", json={"url": "https://jira/browse/PROJ-1"})
    assert client.post("/api/sync/confluence").json()["indexed"] == 1
    assert client.post("/api/sync/jira").json()["indexed"] == 1
    sources = {s["source"]: s for s in client.get("/api/sources").json()}
    assert sources["confluence"]["doc_count"] == 1
    assert sources["jira"]["doc_count"] == 1
    # 미러 파일 존재 (스펙 §13 레이아웃)
    assert list((tmp_path / "data" / "confluence").rglob("*.md"))
    assert (tmp_path / "data" / "jira" / "PROJ-1.md").exists()


def _http_401():
    request = httpx.Request("GET", "https://wiki/x")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)


class Expired401Client:
    """인증이 만료된 상황을 흉내내는 fake — get_page/get_issue가 401 HTTPStatusError를 낸다."""

    def check_auth(self):
        return True

    def get_page(self, page_id):
        raise _http_401()

    def child_page_ids(self, page_id):
        return []

    def get_issue(self, key):
        raise _http_401()


def test_confluence_401_resets_client_and_guides_reauth(tmp_path: Path):
    """스펙 §7.2 P0: 쿠키/인증 만료 시 앱 재시작 없이도 재진단이 가능해야 한다 —
    401을 만나면 캐시된 atlassian_client를 리셋하고 한국어 안내 메시지를 남긴다."""
    client = make_client(tmp_path, atlassian=Expired401Client())
    client.post("/api/atlassian/register",
                json={"url": "https://wiki/pages/viewpage.action?pageId=123"})
    r = client.post("/api/sync/confluence")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "인증이 만료되었습니다" in body["error"]
    assert "다시 동기화" in body["error"]
    state = client.app.state.llmsearch
    assert state["atlassian_client"] is None  # 다음 동기화 때 재진단이 자연히 일어남
    # 안내 문구는 .env 변수명만 언급하고 실제 자격증명 값은 노출하지 않는다
    assert body["error"] == (
        "Atlassian 인증이 만료되었습니다. .env의 자격증명(ATLASSIAN_PAT/USER/PASSWORD/COOKIE)을 "
        "갱신한 뒤 다시 동기화하세요."
    )


def test_jira_401_resets_client_and_guides_reauth(tmp_path: Path):
    client = make_client(tmp_path, atlassian=Expired401Client())
    client.post("/api/atlassian/register", json={"url": "https://jira/browse/PROJ-1"})
    r = client.post("/api/sync/jira")
    body = r.json()
    assert body["ok"] is False
    assert "인증이 만료되었습니다" in body["error"]
    state = client.app.state.llmsearch
    assert state["atlassian_client"] is None


def test_registry_dedups_by_page_id_across_different_url_forms(tmp_path: Path):
    """같은 confluence page_id/jira key가 이미 등록돼 있으면 URL 표기가 달라도(pageId
    쿼리 파라미터 vs /pages/<id> 경로 형태) 중복 추가하지 않는다."""
    registry = Registry(tmp_path / "atlassian.json")
    first = registry.add("https://wiki/pages/viewpage.action?pageId=123")
    second = registry.add("https://wiki/pages/123/제목")  # 같은 page_id, 다른 URL 표기
    assert first["page_id"] == second["page_id"] == "123"
    assert len(registry.list()) == 1


def test_scheduled_sources_skips_confluence_jira_when_registry_empty(tmp_path: Path):
    """registry에 등록이 하나도 없으면 스케줄러는 confluence/jira를 건너뛴다 — 30분마다
    무의미한 인증 오류 로그가 쌓이는 것을 방지한다."""
    state = {"registry": Registry(tmp_path / "atlassian.json")}
    scheduled = _scheduled_sources(state)
    assert "confluence" not in scheduled
    assert "jira" not in scheduled
    assert set(scheduled) == set(SOURCES) - {"confluence", "jira"}


def test_scheduled_sources_includes_confluence_jira_when_registered(tmp_path: Path):
    registry = Registry(tmp_path / "atlassian.json")
    registry.add("https://wiki/pages/viewpage.action?pageId=123")
    registry.add("https://jira/browse/PROJ-1")
    scheduled = _scheduled_sources({"registry": registry})
    assert "confluence" in scheduled and "jira" in scheduled


def test_open_registered_http_url(tmp_path: Path, monkeypatch):
    """M3부터 confluence/jira 문서의 url_or_path는 http(s) URL이다 — 인덱스에 등록된
    URL이면 webbrowser.open으로 열려야 한다 (M1 로컬 경로 전용 Path().resolve() 금지)."""
    client = make_client(tmp_path, atlassian=fake_atlassian())
    client.post("/api/atlassian/register",
                json={"url": "https://wiki/pages/viewpage.action?pageId=123"})
    assert client.post("/api/sync/confluence").json()["indexed"] == 1

    # Windows 환경 시뮬레이션(WSL/리눅스 테스트 환경에서 os.startfile 부재 우회)
    monkeypatch.setattr(os, "startfile", lambda p: None, raising=False)
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    r = client.post("/api/open", json={"url_or_path": "https://wiki/pages/123"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert opened == ["https://wiki/pages/123"]


def test_open_unregistered_http_url_rejected(tmp_path: Path, monkeypatch):
    """인덱스에 등록되지 않은 http(s) URL은 거부된다 — CSRF로 임의 URL을 열게 하는 것 방지."""
    client = make_client(tmp_path, atlassian=fake_atlassian())
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    r = client.post("/api/open", json={"url_or_path": "https://evil.example.com/"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "인덱스" in body["error"]
    assert opened == []


def test_auth_failure_isolated(tmp_path: Path, monkeypatch):
    # 주입 없음 + env 자격증명 없음 → 진단 실패가 로그로 격리
    for var in ("ATLASSIAN_PAT", "ATLASSIAN_USER", "ATLASSIAN_PASSWORD", "ATLASSIAN_COOKIE"):
        monkeypatch.delenv(var, raising=False)
    client = make_client(tmp_path)
    client.post("/api/atlassian/register", json={"url": "https://jira/browse/PROJ-1"})
    r = client.post("/api/sync/jira")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "ATLASSIAN_" in r.json()["error"]
    assert client.post("/api/sync/notes").status_code == 200  # 타 소스 정상
