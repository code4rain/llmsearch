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


def test_broken_symlink_isolation(tmp_path: Path):
    (tmp_path / "good.md").write_text("# 정상", encoding="utf-8")
    (tmp_path / "broken.md").symlink_to(tmp_path / "nonexistent")
    result = sync_notes([tmp_path], [], {})
    assert len(result.documents) == 1
    assert result.documents[0].source_id.endswith("good.md")


def test_unreadable_file_isolation(tmp_path: Path, monkeypatch):
    (tmp_path / "good.md").write_text("# 정상", encoding="utf-8")
    unreadable = tmp_path / "unreadable.md"
    unreadable.write_text("# 불가능", encoding="utf-8")
    r1 = sync_notes([tmp_path], [], {})
    assert len(r1.documents) == 2
    unreadable_sid = str(unreadable.resolve())
    assert unreadable_sid in r1.state["files"]

    def mock_read_text(self, *args, **kwargs):
        if self.name == "unreadable.md":
            raise PermissionError("mock permission denied")
        return self._original_read_text(*args, **kwargs)

    original_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", mock_read_text)
    Path._original_read_text = original_read_text

    # Bump mtime of unreadable file to force read_text() to be reached
    os.utime(unreadable, (time.time() + 10, time.time() + 10))
    r2 = sync_notes([tmp_path], [], r1.state)
    assert len(r2.documents) == 0
    assert unreadable_sid in r2.state["files"]


def test_extra_files_indexed_and_deduped(tmp_path: Path):
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("# 메모A", encoding="utf-8")
    rules = tmp_path / "data" / "rules.md"
    rules.parent.mkdir()
    rules.write_text("# 규칙 (rules.md)\n\n## 용어집\nPJA = 프로젝트A\n", encoding="utf-8")
    result = sync_notes([notes], [], {}, extra_files=[rules, notes / "a.md", tmp_path / "없음.md"])
    ids = [d.source_id for d in result.documents]
    assert len(ids) == 2 and str(rules.resolve()) in ids  # 존재하는 extra만, 폴더 안 파일은 중복 없이
    assert next(d for d in result.documents if d.source_id == str(rules.resolve())).title == "규칙 (rules.md)"
    assert str(rules.resolve()) in result.state["files"]


def test_extra_file_respects_exclude_and_delete(tmp_path: Path):
    rules = tmp_path / "rules.md"
    rules.write_text("# 규칙", encoding="utf-8")
    r1 = sync_notes([], [], {}, extra_files=[rules])
    assert len(r1.documents) == 1
    assert sync_notes([], ["path:*rules.md"], {}, extra_files=[rules]).documents == []
    rules.unlink()
    r2 = sync_notes([], [], r1.state, extra_files=[rules])
    assert r2.deleted_ids == [str(rules.resolve())]
