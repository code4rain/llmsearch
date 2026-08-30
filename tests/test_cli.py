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


def test_search_json_with_fake_embedder(env, capsys):
    _index(env.data)
    code, out, err = _run(["search", "프로젝트A 킥오프 회의록", "--json"], capsys, embedder=EMB)
    assert code == 0
    payload = json.loads(out)
    assert payload["fts_only"] is False and payload["query"] == "프로젝트A 킥오프 회의록"
    hit = payload["hits"][0]
    assert hit["source_id"] == "kickoff.md"
    for key in ("source_type", "title", "url_or_path", "updated_at", "score", "snippet", "excerpt"):
        assert key in hit
    assert "FTS 전용" not in err


def test_search_records_usage_like_gui(env, capsys):
    _index(env.data)
    _run(["search", "킥오프", "--json"], capsys, embedder=EMB)
    assert (env.data / "usage.json").exists()  # CountingEmbedder 경로


def test_search_markdown_has_source_id_and_path(env, capsys):
    _index(env.data)
    code, out, _ = _run(["search", "킥오프 회의록"], capsys, embedder=EMB)
    assert code == 0
    assert "프로젝트A 킥오프" in out and "id: kickoff.md" in out and "/n/kickoff.md" in out
    assert "excerpt" not in out.lower()


def test_search_excerpt_flag(env, capsys):
    _index(env.data)
    _, out, _ = _run(["search", "킥오프 회의록", "--excerpt"], capsys, embedder=EMB)
    assert "> " in out and "일정과 담당자 결정" in out


def test_search_without_key_falls_back_to_fts_with_warning(env, capsys):
    _index(env.data)
    code, out, err = _run(["search", "킥오프 회의록", "--json"], capsys)  # embedder 미주입 + 키 없음
    assert code == 0 and json.loads(out)["fts_only"] is True
    assert "FTS 전용" in err and "하이브리드" in err


def test_search_fts_only_flag_skips_embedder(env, capsys):
    _index(env.data)

    class Boom:
        def embed(self, texts):
            raise AssertionError("호출되면 안 됨")

    code, out, _ = _run(["search", "킥오프", "--fts-only", "--json"], capsys, embedder=Boom())
    assert code == 0 and json.loads(out)["fts_only"] is True


def test_search_filters_forwarded(env, capsys):
    _index(env.data)
    _, out, _ = _run(["search", "프로젝트A", "--source", "local_docs", "--json"], capsys, embedder=EMB)
    hits = json.loads(out)["hits"]
    assert hits and all(h["source_type"] == "local_docs" for h in hits)
    _, out, _ = _run(["search", "회의", "--sender", "kim@corp.com", "--json"], capsys, embedder=EMB)
    assert [h["source_id"] for h in json.loads(out)["hits"]] == ["m1"]
    _, out, _ = _run(["search", "킥오프", "--from", "2027-01-01", "--json"], capsys, embedder=EMB)
    assert json.loads(out)["hits"] == []


def test_search_bad_source_or_date_exit_2(env, capsys):
    _index(env.data)
    code, _, err = _run(["search", "x", "--source", "bogus"], capsys, embedder=EMB)
    assert code == 2 and "bogus" in err
    code, _, err = _run(["search", "x", "--from", "2026/01/01"], capsys, embedder=EMB)
    assert code == 2 and "YYYY-MM-DD" in err
    code, _, err = _run(["search", "x", "--sender", "a@b", "--source", "notes"], capsys, embedder=EMB)
    assert code == 2 and "outlook_mail" in err


def test_search_no_hits_exit_0(env, capsys):
    # 실동작 조정: search.search는 임베더가 주어지면 벡터 후보를 임계값 없이(전량 소규모
    # 코퍼스에서는 사실상 전체 문서) 반환하므로 하이브리드 모드에서는 무관한 질의도 히트가
    # 나온다. "히트 없음" 계약을 결정적으로 검증하려면 벡터 단계를 배제하는 --fts-only가
    # 필요하다(별도 테스트인 test_search_fts_only_flag_skips_embedder가 그 플래그 자체를 검증).
    _index(env.data)
    code, out, _ = _run(["search", "존재하지않는zzz", "--fts-only", "--json"], capsys, embedder=EMB)
    assert code == 0 and json.loads(out)["hits"] == []


def test_get_full_text_json(env, capsys):
    _index(env.data)
    code, out, _ = _run(["get", "notes", "kickoff.md", "--json"], capsys)
    assert code == 0
    p = json.loads(out)
    assert p["title"] == "프로젝트A 킥오프" and p["url_or_path"] == "/n/kickoff.md"
    assert "일정과 담당자 결정" in p["text"] and p["truncated"] is False
    assert p["para_path"] == "Projects/프로젝트A"


def test_get_markdown_and_truncation(env, capsys):
    _index(env.data)
    code, out, _ = _run(["get", "notes", "kickoff.md", "--max-chars", "10"], capsys)
    assert code == 0 and "프로젝트A 킥오프" in out and "--max-chars" in out
    assert len(json.loads(_run(["get", "notes", "kickoff.md", "--max-chars", "10", "--json"], capsys)[1])["text"]) == 10


def test_get_missing_exit_1(env, capsys):
    _index(env.data)
    code, _, err = _run(["get", "notes", "nope.md"], capsys)
    assert code == 1 and "nope.md" in err
