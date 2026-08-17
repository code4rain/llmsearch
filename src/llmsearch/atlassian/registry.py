"""Confluence/Jira URL 등록 저장소 — data_dir/atlassian.json (GUI에서 관리, 스펙 §7.2)."""
from __future__ import annotations

import json
from pathlib import Path

from .urls import parse_atlassian_url


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
                return it  # 중복 등록은 기존 항목 반환
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
