"""실제 Atlassian Server/DC REST 접근 — Confluence v1, Jira v2.

httpx transport 주입으로 MockTransport 단위 테스트 가능 (M2 COM과 달리 WSL 커버).
"""
from __future__ import annotations

import httpx

from .auth import AtlassianAuth

_CHILD_LIMIT = 100


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
        """Jira가 설정돼 있으면 myself, 아니면 Confluence space 목록으로 진단 (한쪽만 설정 가능)."""
        try:
            if self.jira_base:
                return self._http.get(f"{self.jira_base}/rest/api/2/myself").status_code == 200
            resp = self._http.get(f"{self.confluence_base}/rest/api/space", params={"limit": 1})
            return resp.status_code == 200
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
        while True:
            data = self._get(
                f"{self.confluence_base}/rest/api/content/{page_id}/child/page",
                params={"limit": _CHILD_LIMIT, "start": start},
            )
            results = data.get("results", [])
            out.extend(str(r["id"]) for r in results)
            if len(results) < data.get("limit", _CHILD_LIMIT) or not results:
                break
            start += len(results)
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
