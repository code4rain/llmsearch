from datetime import datetime
from pathlib import Path

from llmsearch import db, indexer
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.eval.golden import _matches, evaluate
from llmsearch.models import Document


def test_matches_exact():
	assert _matches("kick.md", "kick.md")


def test_matches_suffix():
	assert _matches("kick.md", "/n/kick.md")
	assert _matches("sub/kick.md", "/notes/sub/kick.md")


def test_matches_no_false_positive():
	assert not _matches("회의록.md", "/n/전사회의록.md")


def test_evaluate(tmp_path: Path):
	conn = db.open_db(tmp_path / "g.db")
	emb = FakeEmbeddings(dim=768)
	indexer.index_documents(conn, [
		Document("notes", "kick.md", "프로젝트A 킥오프", "프로젝트A 킥오프 회의록 8월 1일",
				 "/n/kick.md", datetime(2026, 8, 1)),
		Document("notes", "lunch.md", "점심", "김치찌개", "/n/lunch.md", datetime(2026, 8, 1)),
	], emb)
	report = evaluate(conn, emb, [
		{"question": "프로젝트A 킥오프 언제?", "expect_source_id": "kick.md"},
		{"question": "존재하지 않는 주제 XYZQW", "expect_source_id": "none.md"},
	])
	assert report["total"] == 2
	assert report["hit_at_3"] == 1
	assert report["rate"] == 0.5
	assert report["misses"][0]["question"].startswith("존재하지")


def test_evaluate_reports_rank_per_case(tmp_path: Path):
	from llmsearch.eval.golden import evaluate

	conn = db.open_db(tmp_path / "r.db")
	emb = FakeEmbeddings(dim=768)
	indexer.index_documents(conn, [
		Document("notes", "kick.md", "프로젝트A 킥오프", "프로젝트A 킥오프 회의록 8월 1일", "/n/kick.md", datetime(2026, 8, 1)),
		Document("notes", "lunch.md", "점심", "김치찌개", "/n/lunch.md", datetime(2026, 8, 1)),
	], emb)
	report = evaluate(conn, emb, [
		{"question": "프로젝트A 킥오프 언제?", "expect_source_id": "kick.md"},
		{"question": "존재하지 않는 주제 XYZQW", "expect_source_id": "none.md"},
	])
	assert report["cases"][0]["rank"] == 1 and report["cases"][0]["got"][0] == "kick.md"
	assert report["cases"][1]["rank"] is None
	assert report["misses"] == [{"question": "존재하지 않는 주제 XYZQW", "expected": "none.md", "got": report["cases"][1]["got"]}]
	assert set(report) == {"total", "hit_at_3", "rate", "misses", "cases"}


def test_parse_golden_rules():
	import pytest
	from llmsearch.eval.golden import GOLDEN_MAX_CASES, parse_golden

	assert parse_golden("") == [] and parse_golden("# 주석만\n") == []
	assert parse_golden("- question: q\n  expect_source_id: a.md\n") == [{"question": "q", "expect_source_id": "a.md"}]
	for bad in ("question: q\n", "- q\n", "- question: q\n", "- question: ''\n  expect_source_id: a\n", "- {question: q, expect_source_id: 1}\n"):
		with pytest.raises(ValueError):
			parse_golden(bad)
	with pytest.raises(ValueError, match=str(GOLDEN_MAX_CASES)):
		parse_golden("".join(f"- question: q{i}\n  expect_source_id: a\n" for i in range(GOLDEN_MAX_CASES + 1)))
	with pytest.raises(ValueError):
		parse_golden("- question: [unclosed\n")


def test_main_default_golden_path_and_missing_file(tmp_path: Path, monkeypatch, capsys):
	import llmsearch.eval.golden as g
	from llmsearch.config import Config

	monkeypatch.setattr(g, "load_env", lambda: None)
	monkeypatch.setattr(g, "resolve_config_path", lambda explicit: explicit)
	monkeypatch.setattr(g, "load_config", lambda p: Config(data_dir=tmp_path / "data"))
	monkeypatch.setattr("sys.argv", ["golden", "--config", "c.yaml"])
	import pytest
	with pytest.raises(SystemExit) as ei:
		g.main()
	out = capsys.readouterr().out
	assert ei.value.code == 1 and "golden.yaml이 없습니다" in out
	assert str(tmp_path / "data" / "golden.yaml") in out   # --golden 미지정 시 data_dir 기본값
	assert not (tmp_path / "data" / "index.db").exists()   # 가드가 open_db·임베더 생성보다 앞


def test_main_exits_2_on_config_not_found(tmp_path: Path, monkeypatch, capsys):
	"""설정 파일을 못 찾으면 return이 아니라 sys.exit(2) — 언랩된 __main__에서도 종료코드가 새지 않는다."""
	import pytest

	import llmsearch.eval.golden as g

	monkeypatch.setenv("LLMSEARCH_HOME", str(tmp_path / "nohome"))
	monkeypatch.delenv("LLMSEARCH_CONFIG", raising=False)
	monkeypatch.chdir(tmp_path)
	monkeypatch.setattr("sys.argv", ["golden"])
	with pytest.raises(SystemExit) as ei:
		g.main()
	assert ei.value.code == 2
	assert "설정 파일이 없습니다" in capsys.readouterr().out
