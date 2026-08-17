"""Confluence/Jira URL 등록 저장소 — data_dir/atlassian.json (GUI에서 관리, 스펙 §7.2)."""
from __future__ import annotations

import json
from pathlib import Path

from .urls import parse_atlassian_url


def _same_target(a: dict, b: dict) -> bool:
    """URL 표기가 달라도 같은 confluence page_id / jira key를 가리키는지 비교한다."""
    if a["kind"] == "confluence_page":
        return a.get("page_id") == b.get("page_id")
    if a["kind"] == "jira_issue":
        return a.get("key") == b.get("key")
    return False


class Registry:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, url: str) -> dict:
        parsed = parse_atlassian_url(url)
        if parsed is None:
            raise ValueError(f"인식할 수 없는 Atlassian URL: {url}")
        items = self._load()
        for it in items:
            if it["url"] == url:
                return it  # 완전히 같은 URL 재등록은 기존 항목 반환
            if it["kind"] == parsed["kind"] and _same_target(it, parsed):
                return it  # URL 표기가 달라도(쿼리 vs 경로 형태 등) 같은 page_id/key면 중복 추가 안 함
        items.append(parsed)
        self._save(items)
        return parsed

    def list(self) -> list[dict]:
        return self._load()

    def remove(self, url: str) -> bool:
        items = self._load()
        kept = [it for it in items if it["url"] != url]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True

    def confluence_page_ids(self) -> list[str]:
        return [it["page_id"] for it in self._load() if it["kind"] == "confluence_page"]

    def jira_keys(self) -> list[str]:
        return [it["key"] for it in self._load() if it["kind"] == "jira_issue"]
