"""Atlassian 접근 계약.

페이지 dict: id, space, title, html(storage XHTML), version(int), updated(ISO), ancestors(제목 목록), url
이슈 dict: key, summary, description, status, assignee, updated(ISO), url, comments[{author, created, body}]
구현체는 이 dict 계약을 지켜야 한다. REST 세부는 http_client.py에만 존재한다.
"""
from __future__ import annotations

from typing import Protocol


class AtlassianClient(Protocol):
    def check_auth(self) -> bool: ...

    def get_page(self, page_id: str) -> dict: ...

    def child_page_ids(self, page_id: str) -> list[str]: ...

    def get_issue(self, key: str) -> dict: ...


class FakeAtlassianClient:
    """테스트용 — 프로토콜 시맨틱(KeyError, 빈 자식 목록) 그대로 구현."""

    def __init__(self, pages: dict[str, dict] | None = None,
                 children: dict[str, list[str]] | None = None,
                 issues: dict[str, dict] | None = None, auth_ok: bool = True):
        self.pages = pages or {}
        self.children = children or {}
        self.issues = issues or {}
        self.auth_ok = auth_ok

    def check_auth(self) -> bool:
        return self.auth_ok

    def get_page(self, page_id: str) -> dict:
        return self.pages[page_id]

    def child_page_ids(self, page_id: str) -> list[str]:
        return list(self.children.get(page_id, []))

    def get_issue(self, key: str) -> dict:
        return self.issues[key]
