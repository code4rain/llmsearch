"""Jira 이슈+댓글 커넥터 (스펙 §7.2).

incremental: updated 비교 — 미변경 이슈는 재방출하지 않는다(재임베딩 비용 방지).
미러: mirror_dir/<KEY>.md
삭제 판정 (접근 실패와 삭제 구분):
- KeyError(접근 불가)는 "삭제됨"과 다르다 — 네트워크/권한 일시 장애로도 발생할 수 있다.
- 이전에 알던(prev_updated에 있던) 키에 대한 KeyError는 미스 카운터를 올린다.
  연속 3회 미스에 도달하면 "진짜로 사라짐"으로 확정해 삭제한다.
  다시 성공적으로 가져오면 카운터는 리셋된다.
- 등록 해제된 키(issue_keys에 없는 키)는 접근 실패와 관계없이 즉시 삭제된다.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..atlassian.client import AtlassianClient
from ..models import Document, SyncResult
from ..summarize import _sanitize_segment

MAX_CONSECUTIVE_MISSES = 3  # 이 횟수만큼 연속 KeyError가 나야 "진짜 삭제"로 확정


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return datetime(1970, 1, 1)


def _issue_markdown(issue: dict) -> str:
    lines = [
        f"# [{issue['key']}] {issue['summary']}",
        f"상태: {issue['status']} | 담당: {issue['assignee']} | 갱신: {issue['updated']}",
        "",
        "## 설명",
        issue.get("description") or "(없음)",
    ]
    if issue.get("comments"):
        lines.append("")
        lines.append("## 댓글")
        for c in issue["comments"]:
            lines.append(f"- {c['author']} ({c['created']}): {c['body']}")
    return "\n".join(lines)


def sync_jira(client: AtlassianClient, issue_keys: list[str], state: dict,
              mirror_dir: Path) -> SyncResult:
    prev_updated: dict = dict(state.get("updated", {}))
    prev_mirrors: dict = dict(state.get("mirrors", {}))
    prev_misses: dict = dict(state.get("misses", {}))
    updated: dict[str, str] = {}
    mirrors: dict[str, str] = {}
    misses: dict[str, int] = {}
    documents: list[Document] = []
    expired_misses: set[str] = set()  # 연속 미스 상한 도달 — 삭제 대상

    for key in issue_keys:
        try:
            issue = client.get_issue(key)
        except KeyError:
            if key in prev_updated:
                # 이전에 알던 키의 접근 실패 — 미스 카운터 올리기
                miss = prev_misses.get(key, 0) + 1
                if miss >= MAX_CONSECUTIVE_MISSES:
                    expired_misses.add(key)  # 연속 3회 미스 — 진짜 삭제로 확정
                else:
                    updated[key] = prev_updated[key]
                    mirrors[key] = prev_mirrors[key]
                    misses[key] = miss
            continue

        mirror = mirror_dir / f"{_sanitize_segment(key)}.md"
        updated[key] = issue["updated"]
        mirrors[key] = str(mirror)
        # 성공적으로 다시 가져왔으므로 이전 미스 카운터는 이월하지 않는다(리셋).

        if prev_updated.get(key) == issue["updated"]:
            continue  # 미변경 — 재방출·재기록 없음

        text = _issue_markdown(issue)
        mirror_dir.mkdir(parents=True, exist_ok=True)
        mirror.write_text(text, encoding="utf-8")
        documents.append(Document(
            source_type="jira", source_id=key, title=f"[{key}] {issue['summary']}",
            text=text, url_or_path=issue["url"], updated_at=_parse_dt(issue["updated"]),
            extra={"mirror_path": str(mirror), "status": issue["status"],
                   "assignee": issue["assignee"]},
        ))

    deleted = [k for k in prev_updated if k not in updated and k not in expired_misses]
    deleted.extend(expired_misses)
    for k in deleted:
        old = prev_mirrors.get(k)
        if old and Path(old).exists():
            Path(old).unlink()

    return SyncResult(documents=documents, deleted_ids=deleted,
                      state={"updated": updated, "mirrors": mirrors, "misses": misses})
