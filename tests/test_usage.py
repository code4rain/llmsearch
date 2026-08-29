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
