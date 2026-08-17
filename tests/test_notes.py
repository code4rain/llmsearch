import os
import time
from pathlib import Path

from llmsearch.connectors.notes import sync_notes


def test_initial_sync(tmp_path: Path):
    (tmp_path / "a.md").write_text("# 메모A\n내용", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("# 메모B", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("md 아님", encoding="utf-8")
    result = sync_notes([tmp_path], [], {})
    ids = {d.source_id for d in result.documents}
    assert len(ids) == 2 and all(i.endswith(".md") for i in ids)
    doc = next(d for d in result.documents if d.source_id.endswith("a.md"))
    assert doc.title == "메모A"  # 첫 헤딩을 제목으로
    assert doc.source_type == "notes"


def test_incremental_and_delete(tmp_path: Path):
    f = tmp_path / "a.md"
    f.write_text("v1", encoding="utf-8")
    r1 = sync_notes([tmp_path], [], {})
    # 변화 없음 → 빈 결과
    r2 = sync_notes([tmp_path], [], r1.state)
    assert r2.documents == [] and r2.deleted_ids == []
    # 수정 → 재수집
    os.utime(f, (time.time() + 10, time.time() + 10))
    r3 = sync_notes([tmp_path], [], r2.state)
    assert len(r3.documents) == 1
    # 삭제 → deleted_ids
    f.unlink()
    r4 = sync_notes([tmp_path], [], r3.state)
    assert len(r4.deleted_ids) == 1


def test_exclude(tmp_path: Path):
    (tmp_path / "비밀").mkdir()
    (tmp_path / "비밀" / "s.md").write_text("x", encoding="utf-8")
    result = sync_notes([tmp_path], ["path:**/비밀/**"], {})
    assert result.documents == []
