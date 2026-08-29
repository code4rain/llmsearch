import json
from datetime import date
from pathlib import Path

from llmsearch import usage
from llmsearch.usage import UsageTracker


def test_record_accumulates_and_persists(tmp_path: Path):
    t = UsageTracker(tmp_path / "usage.json")
    t.record("embed")
    t.record("embed", 2)
    t.record("summary")
    assert t.today_total() == 4
    saved = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    today = date.today().isoformat()
    assert saved[today] == {"embed": 3, "summary": 1}


def test_reload_from_disk(tmp_path: Path):
    t1 = UsageTracker(tmp_path / "usage.json")
    t1.record("embed", 5)
    t2 = UsageTracker(tmp_path / "usage.json")
    assert t2.today_total() == 5


def test_limit_zero_is_unlimited(tmp_path: Path):
    t = UsageTracker(tmp_path / "usage.json", daily_limit=0)
    t.record("embed", 10_000)
    assert t.indexing_allowed() is True


def test_limit_reached_blocks_indexing(tmp_path: Path):
    t = UsageTracker(tmp_path / "usage.json", daily_limit=3)
    t.record("embed", 2)
    assert t.indexing_allowed() is True  # 2 < 3
    t.record("summary")
    assert t.indexing_allowed() is False  # 3 >= 3


def test_day_rollover_resets_today(tmp_path: Path, monkeypatch):
    """어제 카운트는 오늘 합계·상한 판정에 영향을 주지 않는다."""
    t = UsageTracker(tmp_path / "usage.json", daily_limit=3)
    t.record("embed", 3)
    assert t.indexing_allowed() is False

    class Tomorrow:
        @staticmethod
        def today():
            return date.fromordinal(date.today().toordinal() + 1)

    monkeypatch.setattr(usage, "date", Tomorrow)
    assert t.today_total() == 0
    assert t.indexing_allowed() is True


def test_old_days_pruned(tmp_path: Path):
    path = tmp_path / "usage.json"
    stale = {f"2020-01-{d:02d}": {"embed": 1} for d in range(1, 32)}
    path.write_text(json.dumps(stale), encoding="utf-8")
    t = UsageTracker(path)
    t.record("embed")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) <= usage._KEEP_DAYS


def test_corrupt_file_starts_fresh(tmp_path: Path):
    path = tmp_path / "usage.json"
    path.write_text("{broken", encoding="utf-8")
    t = UsageTracker(path)
    assert t.today_total() == 0
    t.record("embed")  # 손상 파일 위에도 정상 기록
    assert t.today_total() == 1


def test_wrong_shape_list_starts_fresh(tmp_path: Path):
    """유효한 JSON이지만 리스트 형태면 새로 시작한다."""
    path = tmp_path / "usage.json"
    path.write_text(json.dumps([]), encoding="utf-8")
    t = UsageTracker(path)
    assert t.today_total() == 0
    t.record("embed")
    assert t.today_total() == 1


def test_wrong_shape_null_starts_fresh(tmp_path: Path):
    """유효한 JSON이지만 null이면 새로 시작한다."""
    path = tmp_path / "usage.json"
    path.write_text(json.dumps(None), encoding="utf-8")
    t = UsageTracker(path)
    assert t.today_total() == 0
    t.record("embed")
    assert t.today_total() == 1


def test_wrong_shape_dict_with_int_values_starts_fresh(tmp_path: Path):
    """유효한 JSON이지만 dict 값이 dict가 아니면 새로 시작한다."""
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"2026-08-29": 5}), encoding="utf-8")
    t = UsageTracker(path)
    assert t.today_total() == 0
    t.record("embed")
    assert t.today_total() == 1


def test_counting_embedder_records_and_delegates(tmp_path: Path):
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.usage import CountingEmbedder

    t = UsageTracker(tmp_path / "usage.json")
    e = CountingEmbedder(FakeEmbeddings(), t)
    out = e.embed(["가", "나"])
    assert len(out) == 2 and len(out[0]) > 0  # 위임 결과 그대로
    e.embed(["다"])
    assert t.today_total() == 2  # 호출 단위 기록 (텍스트 수 아님 — 배치 1회 = API 1회)


def test_counting_embedder_records_even_at_limit(tmp_path: Path):
    """래퍼는 차단하지 않는다 — 상한 도달 후에도 검색 쿼리 임베딩은 동작해야 한다."""
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.usage import CountingEmbedder

    t = UsageTracker(tmp_path / "usage.json", daily_limit=1)
    e = CountingEmbedder(FakeEmbeddings(), t)
    e.embed(["가"])
    assert t.indexing_allowed() is False
    assert len(e.embed(["나"])) == 1  # 차단 없이 정상 위임


def test_counting_summarizer_records_by_kind(tmp_path: Path):
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.usage import CountingSummarizer

    t = UsageTracker(tmp_path / "usage.json")
    s = CountingSummarizer(FakeSummarizer(), t)
    r = s.summarize_and_classify(title="문서", text="프로젝트A 내용", projects=["프로젝트A"],
                                 areas=[], existing_resources=[], prior_category=None,
                                 glossary="", rules="")
    assert r.category == "Projects/프로젝트A"  # 위임 결과 그대로
    assert "파일명 기반" in s.describe_filename("보고서.pptx")
    assert "2장" in s.describe_images("덱.pptx", [b"a", b"b"])
    saved = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    today = date.today().isoformat()
    assert saved[today] == {"summary": 2, "vision": 1}
