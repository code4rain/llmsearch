"""Confluence 페이지+하위 트리 커넥터 (스펙 §7.2).

증분: version 비교 — 미변경 페이지는 재방출하지 않는다(재임베딩 비용 방지).
미러: mirror_dir/<space>/<조상...>/<제목>__<id>.md — __<id> 접미사로 동명 충돌 방지.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path

from ..atlassian.client import AtlassianClient
from ..atlassian.htmlmd import html_to_markdown
from ..models import Document, SyncResult
from ..summarize import _sanitize_segment

MAX_PAGES_PER_TREE = 500  # 폭주 방지 상한 — 초과분은 다음 스펙 개정에서 페이징


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return datetime(1970, 1, 1)


def _mirror_path(mirror_dir: Path, page: dict) -> Path:
    parts = [_sanitize_segment(page["space"])] + [_sanitize_segment(a) for a in page["ancestors"]]
    name = f"{_sanitize_segment(page['title'])}__{page['id']}.md"
    return mirror_dir.joinpath(*parts, name)


def _page_document(page: dict, mirror: Path) -> Document:
    md = html_to_markdown(page["html"])
    text = f"# {page['title']}\n(스페이스: {page['space']})\n\n{md}"
    return Document(
        source_type="confluence", source_id=page["id"], title=page["title"],
        text=text, url_or_path=page["url"], updated_at=_parse_dt(page["updated"]),
        extra={"mirror_path": str(mirror), "space": page["space"]},
    )


def sync_confluence(client: AtlassianClient, page_ids: list[str], state: dict,
                    mirror_dir: Path) -> SyncResult:
    prev_versions: dict = dict(state.get("versions", {}))
    prev_mirrors: dict = dict(state.get("mirrors", {}))
    versions: dict[str, int] = {}
    mirrors: dict[str, str] = {}
    documents: list[Document] = []
    visited: set[str] = set()

    for root in page_ids:
        queue: deque[str] = deque([root])
        count = 0
        while queue and count < MAX_PAGES_PER_TREE:
            pid = queue.popleft()
            if pid in visited:
                continue
            visited.add(pid)
            count += 1
            try:
                page = client.get_page(pid)
            except KeyError:
                continue  # 접근 불가 페이지는 건너뛰고 트리 나머지 계속 (부분 격리)
            queue.extend(client.child_page_ids(pid))

            mirror = _mirror_path(mirror_dir, page)
            versions[pid] = page["version"]
            mirrors[pid] = str(mirror)
            if prev_versions.get(pid) == page["version"]:
                continue  # 미변경 — 재방출·재기록 없음
            doc = _page_document(page, mirror)
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text(doc.text, encoding="utf-8")
            old = prev_mirrors.get(pid)
            if old and old != str(mirror) and Path(old).exists():
                Path(old).unlink()  # 제목/조상 변경으로 경로 이동 시 이전 미러 정리
            documents.append(doc)

    deleted = [pid for pid in prev_versions if pid not in versions]
    for pid in deleted:
        old = prev_mirrors.get(pid)
        if old and Path(old).exists():
            Path(old).unlink()

    return SyncResult(documents=documents, deleted_ids=deleted,
                      state={"versions": versions, "mirrors": mirrors})
