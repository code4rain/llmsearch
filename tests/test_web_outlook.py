import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from llmsearch.config import Config
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.llm import FakeAnswerer
from llmsearch.outlook.client import FakeOutlookClient
from llmsearch.summarize import FakeSummarizer
from llmsearch.web.app import create_app

NOW = datetime.now()


def make_client(tmp_path: Path, outlook=None) -> TestClient:
    cfg = Config(data_dir=tmp_path / "data")
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), outlook_client=outlook, enable_scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1")


def fake_outlook():
    return FakeOutlookClient(mails={"inbox": [{
        "entry_id": "m1", "subject": "프로젝트A 결정사항", "body": "회의 결과 공유",
        "sender_name": "김철수", "sender_email": "kim@corp.com",
        "received_at": NOW - timedelta(days=1), "folder": "inbox",
    }]}, appointments=[{
        "entry_id": "e1", "subject": "주간 회의", "body": "", "location": "회의실",
        "start": NOW + timedelta(days=2), "end": NOW + timedelta(days=2, hours=1),
        "attendees": "나",
    }])


def test_outlook_sources_listed(tmp_path: Path):
    client = make_client(tmp_path, outlook=fake_outlook())
    sources = {s["source"] for s in client.get("/api/sources").json()}
    assert {"outlook_mail", "outlook_cal"} <= sources


def test_mail_sync_and_search(tmp_path: Path):
    client = make_client(tmp_path, outlook=fake_outlook())
    r = client.post("/api/sync/outlook_mail")
    assert r.status_code == 200 and r.json()["indexed"] == 1
    mail_status = next(s for s in client.get("/api/sources").json() if s["source"] == "outlook_mail")
    assert mail_status["doc_count"] == 1
    assert mail_status["backlog"] is False


def test_static_index_html_renders_backlog_badge():
    """FINDING 3: backlog=true sources must show a visible badge in the sources tab.

    API-level backlog field coverage lives in test_mail_sync_and_search; this checks
    the static template actually renders something for it.
    """
    html_path = Path(__file__).parent.parent / "src" / "llmsearch" / "web" / "static" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    assert "s.backlog" in html
    assert "수집 중" in html


def test_cal_sync(tmp_path: Path):
    client = make_client(tmp_path, outlook=fake_outlook())
    assert client.post("/api/sync/outlook_cal").json()["indexed"] == 1


def test_outlook_unavailable_isolated(tmp_path: Path):
    client = make_client(tmp_path, outlook=FakeOutlookClient(available=False))
    r = client.post("/api/sync/outlook_mail")
    assert r.status_code == 200
    assert r.json()["ok"] is False and "Outlook" in r.json()["error"]
    # 다른 소스는 정상 동작
    assert client.post("/api/sync/notes").status_code == 200


def test_open_outlook_item(tmp_path: Path):
    fake = fake_outlook()
    client = make_client(tmp_path, outlook=fake)
    r = client.post("/api/open", json={"url_or_path": "outlook:m1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert fake.opened == ["m1"]


def test_open_unindexed_local_path_rejected(tmp_path: Path):
    """인덱스에 등록되지 않은 로컬 경로는 어느 플랫폼에서도 열기가 거부된다 — CSRF로
    임의 사이트가 /api/open을 두드려도 인덱스 밖 파일은 실행될 수 없어야 한다."""
    client = make_client(tmp_path)
    f = tmp_path / "a.md"; f.write_text("x", encoding="utf-8")
    r = client.post("/api/open", json={"url_or_path": str(f)})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "인덱스" in body["error"]


def test_open_indexed_local_path(tmp_path: Path, monkeypatch):
    """인덱스에 등록된 경로만 os.startfile로 열린다 — 테스트는 startfile을 fake로 격리해
    실제 OS 열기 부작용 없이 어느 플랫폼에서나 실행 가능하다."""
    notes = tmp_path / "notes"; notes.mkdir()
    note_file = notes / "kick.md"
    note_file.write_text("# 킥오프\n내용", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.post("/api/sync/notes").json()["indexed"] == 1

    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)

    r = client.post("/api/open", json={"url_or_path": str(note_file)})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert opened == [str(note_file.resolve())]
