"""실제 Atlassian Server/DC REST 접근 — Confluence v1, Jira v2.

httpx transport 주입으로 MockTransport 단위 테스트 가능 (M2 COM과 달리 WSL 커버).
"""
from __future__ import annotations

import httpx

from .auth import AtlassianAuth

_CHILD_LIMIT = 100
_MAX_CHILD_PAGES = 20  # 하드 캡: limit=100일 때 최대 2000개 자식, MAX_PAGES_PER_TREE=500 훨씬 초과
# 이 절단(최대 2000개)은 connectors/confluence.py의 safe/unsafe 라운드 판정에는 보이지
# 않는다(child_page_ids는 그냥 잘린 목록을 반환할 뿐 truncated를 알리지 않음) — 그래도
# 안전한 이유는 2000 ≫ MAX_PAGES_PER_TREE(500)라 트리 순회가 항상 confluence.py 쪽
# 상한에 먼저 걸리기 때문이다. 두 상수를 조정할 때는 이 관계를 유지할 것.


class HttpAtlassianClient:
    def __init__(self, confluence_base: str, jira_base: str, auth: AtlassianAuth,
                 transport=None, timeout: float = 30.0):
        self.confluence_base = confluence_base.rstrip("/")
        self.jira_base = jira_base.rstrip("/")
        headers = {}
        basic_auth = None
        if auth.mode == "pat":
            headers["Authorization"] = f"Bearer {auth.token}"
        elif auth.mode == "cookie":
            headers["Cookie"] = auth.cookie
        elif auth.mode == "basic":
            basic_auth = (auth.user, auth.password)
        self._http = httpx.Client(headers=headers, auth=basic_auth,
                                  timeout=timeout, transport=transport)

    def _get(self, url: str, params: dict | None = None) -> dict:
        resp = self._http.get(url, params=params)
        if resp.status_code in (403, 404):
            raise KeyError(url)
        resp.raise_for_status()
        return resp.json()

    def check_auth(self) -> bool:
        """설정된 서비스(Confluence/Jira) 전부를 프로브해 전부 성공해야 인증 유효로 본다.

        base URL이 하나만 설정돼 있으면 그 서비스만 프로브한다. 두 base가 모두 설정된
        경우 하나만 프로브하면(예: jira만) confluence 쪽 인증이 이미 만료됐어도 True를
        반환하는 2-서버 부정합이 생긴다 — 단일 자격증명이 두 서버 모두에 유효해야
        한다는 전제이므로(README 참고) 둘 다 확인해야 한다.
        """
        try:
            if self.confluence_base:
                resp = self._http.get(f"{self.confluence_base}/rest/api/space", params={"limit": 1})
                if resp.status_code != 200:
                    return False
            if self.jira_base:
                resp = self._http.get(f"{self.jira_base}/rest/api/2/myself")
                if resp.status_code != 200:
                    return False
            return True
        except httpx.HTTPError:
            return False

    def get_page(self, page_id: str) -> dict:
        data = self._get(
            f"{self.confluence_base}/rest/api/content/{page_id}",
            params={"expand": "body.storage,version,space,ancestors"},
        )
        webui = data.get("_links", {}).get("webui", f"/pages/viewpage.action?pageId={page_id}")
        return {
            "id": str(data["id"]),
            "space": data.get("space", {}).get("key", ""),
            "title": data.get("title", "(제목 없음)"),
            "html": data.get("body", {}).get("storage", {}).get("value", ""),
            "version": int(data.get("version", {}).get("number", 0)),
            "updated": str(data.get("version", {}).get("when", ""))[:19],
            "ancestors": [a.get("title", "") for a in data.get("ancestors", [])],
            "url": f"{self.confluence_base}{webui}",
        }

    def child_page_ids(self, page_id: str) -> list[str]:
        out: list[str] = []
        start = 0
        page_count = 0
        while page_count < _MAX_CHILD_PAGES:
            data = self._get(
                f"{self.confluence_base}/rest/api/content/{page_id}/child/page",
                params={"limit": _CHILD_LIMIT, "start": start},
            )
            results = data.get("results", [])
            out.extend(str(r["id"]) for r in results)
            if len(results) < data.get("limit", _CHILD_LIMIT) or not results:
                break
            start += len(results)
            page_count += 1
        return out

    def get_issue(self, key: str) -> dict:
        data = self._get(
            f"{self.jira_base}/rest/api/2/issue/{key}",
            params={"fields": "summary,description,status,assignee,updated,comment"},
        )
        f = data.get("fields", {})
        comment_block = f.get("comment") or {}
        return {
            "key": data["key"],
            "summary": f.get("summary", ""),
            "description": f.get("description") or "",
            "status": (f.get("status") or {}).get("name", ""),
            "assignee": (f.get("assignee") or {}).get("displayName", ""),
            "updated": str(f.get("updated", ""))[:19],
            "url": f"{self.jira_base}/browse/{data['key']}",
            "comments": [
                {"author": (c.get("author") or {}).get("displayName", ""),
                 "created": str(c.get("created", ""))[:19], "body": c.get("body", "")}
                for c in comment_block.get("comments", [])
            ],
        }
