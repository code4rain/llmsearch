import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmsearch import cli, db, indexer
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.models import Document

EMB = FakeEmbeddings(dim=768)


def _index(data_dir: Path):
    conn = db.open_db(data_dir / "index.db")
    now = datetime(2026, 8, 15)
    docs = [
        Document("notes", "kickoff.md", "프로젝트A 킥오프", "프로젝트A 킥오프 회의록. 일정과 담당자 결정.",
                 "/n/kickoff.md", now, extra={"para_path": "Projects/프로젝트A"}),
        Document("notes", "lunch.md", "점심 기록", "오늘 점심은 김치찌개.", "/n/lunch.md", now),
        Document("local_docs", "spec.pptx", "프로젝트A 발표자료", "프로젝트A 발표자료 요약. 로드맵 포함.",
                 "/d/spec.pptx", now),
        Document("outlook_mail", "m1", "회의 안내", "프로젝트A 회의 안내 메일.", "outlook:m1", now,
                 extra={"sender": "kim@corp.com"}),
    ]
    indexer.index_documents(conn, docs, EMB)
    indexer.set_sync_state(conn, "notes", {"files": {}})
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """전역 설정이 tmp를 가리키고, cwd·HOME에 .env가 없어 GEMINI 키가 비어 있는 환경."""
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"data_dir: {data}\n", encoding="utf-8")
    monkeypatch.setenv("LLMSEARCH_CONFIG", str(cfg))
    monkeypatch.setenv("LLMSEARCH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(cfg=cfg, data=data)


def _run(argv, capsys, **kw):
    code = cli.main(argv, **kw)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_status_json(env, capsys):
    _index(env.data)
    code, out, _ = _run(["status", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["db"] == str(env.data / "index.db")
    assert payload["schema_version"] == db.SCHEMA_VERSION
    assert payload["vector_backend"] in ("sqlite-vec", "numpy")
    by = {s["source"]: s for s in payload["sources"]}
    assert by["notes"]["doc_count"] == 2 and by["notes"]["synced"] is True
    assert by["jira"]["doc_count"] == 0 and by["jira"]["synced"] is False
    assert payload["usage_today"] == 0 and payload["rebuild_in_progress"] is False


def test_status_markdown_mentions_counts(env, capsys):
    _index(env.data)
    code, out, _ = _run(["status"], capsys)
    assert code == 0 and "notes" in out and "| 2 |" in out


def test_missing_config_exit_2(env, capsys, monkeypatch):
    monkeypatch.setenv("LLMSEARCH_CONFIG", str(env.cfg.parent / "nope.yaml"))
    code, _, err = _run(["status"], capsys)
    assert code == 2 and "install.sh" in err


def test_missing_index_exit_2_without_creating(env, capsys):
    code, _, err = _run(["status"], capsys)
    assert code == 2 and "sync all" in err
    assert not (env.data / "index.db").exists()  # open_db가 빈 DB를 만들지 않았다


def test_schema_mismatch_exit_4(env, capsys):
    _index(env.data)
    import sqlite3
    conn = sqlite3.connect(env.data / "index.db")
    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    code, _, err = _run(["status"], capsys)
    assert code == 4 and "재구축" in err
