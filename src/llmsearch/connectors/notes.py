from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ..models import Document, SyncResult
from ..rules import is_excluded


def _title_of(path: Path, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem


def _candidates(folders: list[Path], extra_files: Sequence[Path]) -> list[Path]:
    out: list[Path] = []
    for folder in folders:
        if folder.exists():
            out.extend(sorted(folder.rglob("*.md")))
    # rules.md 같은 단일 파일 — 존재하는 것만 (스펙 §9 "rules.md는 notes로 취급되어 인덱싱")
    out.extend(p for p in extra_files if p.exists())
    return out


def sync_notes(folders: list[Path], excludes: list[str], state: dict,
               extra_files: Sequence[Path] = ()) -> SyncResult:
    prev: dict[str, float] = dict(state.get("files", {}))
    seen: dict[str, float] = {}
    documents: list[Document] = []
    for path in _candidates(folders, extra_files):
        sid = str(path.resolve())
        if sid in seen:
            continue  # extra_files가 폴더 안 파일을 가리키면 중복 임베딩 방지
        if is_excluded(sid, None, path.parent.name, excludes):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        seen[sid] = mtime
        if prev.get(sid) == mtime:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        documents.append(
            Document(
                source_type="notes", source_id=sid, title=_title_of(path, text),
                text=text, url_or_path=sid,
                updated_at=datetime.fromtimestamp(mtime),
            )
        )
    deleted = [sid for sid in prev if sid not in seen]
    return SyncResult(documents=documents, deleted_ids=deleted, state={"files": seen})
