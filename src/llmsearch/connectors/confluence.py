"""Confluence 페이지+하위 트리 커넥터 (스펙 §7.2).

증분: version 비교 — 미변경 페이지는 재방출하지 않는다(재임베딩 비용 방지).
미러: mirror_dir/<space>/<조상...>/<제목>__<id>.md — __<id> 접미사로 동명 충돌 방지.

삭제 판정 (접근 실패와 삭제 구분):
- KeyError(접근 불가)는 "삭제됨"과 다르다 — 네트워크/권한 일시 장애로도 발생할 수 있다.
  이번 순회에서 KeyError가 한 번이라도 나거나(had_failures) 어느 루트든
  MAX_PAGES_PER_TREE에서 잘렸으면(truncated) 그 순회는 "UNSAFE 라운드"다. UNSAFE
  라운드에서는 이번 순회에 없던 이전 page_id를 삭제하지 않고 이전 상태(version+미러
  경로)를 그대로 이월한다 — 잘린/실패한 지점 아래 자식들은 애초에 enqueue되지 않아
  하위 트리 전체가 잘못 삭제 판정될 수 있기 때문이다.
- 이전에 알던(prev_versions에 있던) 페이지에 대한 KeyError는 미스 카운터
  (`state["misses"][pid]`)를 올린다. 연속 3회 미스에 도달하면 "진짜로 사라짐"으로
  확정해 이월을 멈추고 일반 삭제 스윕에 포함시킨다 — 이 경우는 라운드의 SAFE/UNSAFE
  여부와 무관하게 삭제된다. 다시 성공적으로 가져오면 카운터는 사라진다(리셋).
- SAFE 라운드(실패도 잘림도 없음)에서만 "이번 순회에 없는 이전 page_id"를 일반
  삭제(deleted_ids + 미러 정리) 처리한다.
- 미러 파일을 지운 뒤(경로 이동으로 인한 정리, 삭제 스윕 모두)에는 mirror_dir을
  넘지 않는 범위에서 비어버린 상위 디렉터리를 위로 정리한다.
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
MAX_CONSECUTIVE_MISSES = 3  # 이 횟수만큼 연속 KeyError가 나야 "진짜 삭제"로 확정


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


def _prune_empty_dirs(leaf: Path, mirror_dir: Path) -> None:
    """leaf(방금 지운 파일)의 상위 디렉터리들을 mirror_dir 직전까지 위로 정리한다.

    빈 디렉터리만 지워진다 — rmdir은 비어있지 않으면 OSError를 내므로, 그 시점에서
    멈춘다(형제 파일/디렉터리가 남아있는 상위 폴더는 건드리지 않음).
    """
    d = leaf.parent
    while d != mirror_dir and d != d.parent:
        try:
            d.rmdir()
        except OSError:
            break
        d = d.parent


def sync_confluence(client: AtlassianClient, page_ids: list[str], state: dict,
                    mirror_dir: Path) -> SyncResult:
    prev_versions: dict = dict(state.get("versions", {}))
    prev_mirrors: dict = dict(state.get("mirrors", {}))
    prev_misses: dict = dict(state.get("misses", {}))
    versions: dict[str, int] = {}
    mirrors: dict[str, str] = {}
    misses: dict[str, int] = {}
    documents: list[Document] = []
    visited: set[str] = set()
    expired_misses: set[str] = set()  # 연속 미스 상한 도달 — 라운드 안전 여부와 무관하게 삭제
    had_failures = False
    truncated = False

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
                had_failures = True
                if pid in prev_versions:
                    miss = prev_misses.get(pid, 0) + 1
                    if miss >= MAX_CONSECUTIVE_MISSES:
                        expired_misses.add(pid)  # 연속 3회 미스 — 진짜 삭제로 확정
                    else:
                        versions[pid] = prev_versions[pid]
                        mirrors[pid] = prev_mirrors[pid]
                        misses[pid] = miss
                continue  # 접근 불가 페이지는 건너뛰고 트리 나머지 계속 (부분 격리)
            queue.extend(client.child_page_ids(pid))

            mirror = _mirror_path(mirror_dir, page)
            versions[pid] = page["version"]
            mirrors[pid] = str(mirror)
            # 성공적으로 다시 가져왔으므로 이전 미스 카운터는 이월하지 않는다(리셋).
            if prev_versions.get(pid) == page["version"]:
                continue  # 미변경 — 재방출·재기록 없음
            doc = _page_document(page, mirror)
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text(doc.text, encoding="utf-8")
            old = prev_mirrors.get(pid)
            if old and old != str(mirror) and Path(old).exists():
                Path(old).unlink()  # 제목/조상 변경으로 경로 이동 시 이전 미러 정리
                _prune_empty_dirs(Path(old), mirror_dir)
            documents.append(doc)

        if queue:
            truncated = True  # 이 루트는 상한에 걸려 잘렸다 — 남은 큐가 있다는 뜻

    unsafe_round = truncated or had_failures
    deleted: list[str] = []
    for pid in prev_versions:
        if pid in versions:
            continue  # 이번에 방문했거나(성공/미스<3 이월) 이미 처리됨
        if pid in expired_misses:
            deleted.append(pid)  # 연속 미스 상한 도달 — 라운드 안전 여부 무관하게 삭제
            continue
        if unsafe_round:
            # 실패/잘림이 있었던 라운드에서는 "안 보인 페이지"를 삭제로 오판하지 않고
            # 이전 상태를 그대로 이월한다(그 하위 트리는 애초에 순회되지 않았을 수 있음).
            versions[pid] = prev_versions[pid]
            mirrors[pid] = prev_mirrors[pid]
            if pid in prev_misses:
                misses[pid] = prev_misses[pid]
            continue
        deleted.append(pid)  # SAFE 라운드에서 안 보인 페이지 — 진짜로 삭제됨

    for pid in deleted:
        old = prev_mirrors.get(pid)
        if old and Path(old).exists():
            Path(old).unlink()
            _prune_empty_dirs(Path(old), mirror_dir)

    return SyncResult(documents=documents, deleted_ids=deleted,
                      state={"versions": versions, "mirrors": mirrors, "misses": misses})
