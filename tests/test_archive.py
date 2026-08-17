import json
from pathlib import Path

import pytest

from llmsearch import db, indexer
from llmsearch.archive import archive_project


def _setup(tmp_path: Path):
    conn = db.open_db(tmp_path / "index.db")
    summaries = tmp_path / "summaries"
    proj = summaries / "Projects" / "알파"
    proj.mkdir(parents=True)
    summary = proj / "보고서.pptx.md"
    summary.write_text("# 요약", encoding="utf-8")
    (proj / "보고서.pptx").write_bytes(b"orig")
    conn.execute(
        "INSERT INTO documents(source_type, source_id, title, url_or_path, updated_at,"
        " content_indexed, para_path, extra_json) VALUES (?,?,?,?,?,?,?,?)",
        ("local_docs", "C:\\docs\\보고서.pptx", "보고서.pptx", "C:\\docs\\보고서.pptx",
         "2026-08-01T00:00:00", 1, "Projects/알파",
         json.dumps({"para_path": "Projects/알파", "summary_path": str(summary)},
                    ensure_ascii=False)),
    )
    indexer.set_para_map(conn, "C:\\docs\\보고서.pptx", "Projects/알파", str(summary))
    conn.commit()
    return conn, summaries


def test_archive_moves_folder_and_updates_index(tmp_path: Path):
    conn, summaries = _setup(tmp_path)
    out = archive_project(conn, summaries, "알파")
    assert out["documents"] == 1 and out["mappings"] == 1
    assert not (summaries / "Projects" / "알파").exists()
    new_summary = summaries / "Archives" / "알파" / "보고서.pptx.md"
    assert new_summary.exists() and (summaries / "Archives" / "알파" / "보고서.pptx").exists()
    row = conn.execute("SELECT para_path, extra_json FROM documents").fetchone()
    assert row[0] == "Archives/알파"
    extra = json.loads(row[1])
    assert extra["para_path"] == "Archives/알파" and extra["summary_path"] == str(new_summary)
    pm = indexer.get_para_map(conn, "C:\\docs\\보고서.pptx")
    assert pm == ("Archives/알파", str(new_summary))
    assert "config.yaml" in out["hint"] and "알파" in out["hint"]


def test_archive_unknown_project_raises(tmp_path: Path):
    conn, summaries = _setup(tmp_path)
    with pytest.raises(KeyError):
        archive_project(conn, summaries, "없음")


def test_archive_rejects_bad_name(tmp_path: Path):
    conn, summaries = _setup(tmp_path)
    with pytest.raises(ValueError):
        archive_project(conn, summaries, "..")
    with pytest.raises(ValueError):
        archive_project(conn, summaries, "a/b")


def test_archive_target_exists_raises(tmp_path: Path):
    conn, summaries = _setup(tmp_path)
    (summaries / "Archives" / "알파").mkdir(parents=True)
    with pytest.raises(ValueError):
        archive_project(conn, summaries, "알파")
    assert (summaries / "Projects" / "알파").exists()  # 원본 그대로


def test_archive_rolls_back_move_on_db_failure(tmp_path: Path, monkeypatch):
    """SQL 갱신이 실패하면 폴더 이동을 되돌린다 — 파일/인덱스 불일치 방지."""
    conn, summaries = _setup(tmp_path)
    conn.close()  # 닫힌 커넥션 → UPDATE에서 ProgrammingError
    import sqlite3

    with pytest.raises(sqlite3.ProgrammingError):
        archive_project(conn, summaries, "알파")
    assert (summaries / "Projects" / "알파").exists()
    assert not (summaries / "Archives" / "알파").exists()
