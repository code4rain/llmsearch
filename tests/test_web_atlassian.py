from pathlib import Path

from fastapi.testclient import TestClient

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
