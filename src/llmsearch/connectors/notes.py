from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..models import Document, SyncResult
from ..rules import is_excluded


def _title_of(path: Path, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem


def sync_notes(folders: list[Path], excludes: list[str], state: dict) -> SyncResult:
    prev: dict[str, float] = dict(state.get("files", {}))
    seen: dict[str, float] = {}
    documents: list[Document] = []
    for folder in folders:
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.md")):
            sid = str(path.resolve())
            if is_excluded(sid, None, path.parent.name, excludes):
                continue
            mtime = path.stat().st_mtime
            seen[sid] = mtime
            if prev.get(sid) == mtime:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            documents.append(
                Document(
                    source_type="notes", source_id=sid, title=_title_of(path, text),
                    text=text, url_or_path=sid,
                    updated_at=datetime.fromtimestamp(mtime),
                )
            )
    deleted = [sid for sid in prev if sid not in seen]
    return SyncResult(documents=documents, deleted_ids=deleted, state={"files": seen})
