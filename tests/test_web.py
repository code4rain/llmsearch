import json
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
    return TestClient(app)


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
