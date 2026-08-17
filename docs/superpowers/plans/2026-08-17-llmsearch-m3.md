# llmsearch M3 구현 계획 (Confluence/Jira)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스펙 M3 — Confluence 페이지+하위 트리·Jira 이슈+댓글 커넥터(인증 3단 폴백 자동 진단, 로컬 Markdown 미러, 증분 동기화, GUI URL 등록). 완료 시 5개 소스 전체가 통합 검색에 합류한다.

**Architecture:** M2와 동일한 격리 패턴. REST 의존을 `AtlassianClient` 프로토콜(순수 dict 계약) 뒤로 격리 — 커넥터는 프로토콜만 소비해 WSL에서 Fake로 완전 테스트. 실 구현 `HttpAtlassianClient`는 httpx 기반으로 **MockTransport 주입으로 단위 테스트 가능**(M2의 COM과 달리 실클라이언트도 WSL 테스트 커버). 인증은 후보(PAT→Basic→쿠키)를 순서대로 `check_auth()` 진단해 첫 성공을 채택(스펙 §7.2 P0). URL 등록은 `data_dir/atlassian.json` 레지스트리로 GUI에서 관리.

**Tech Stack:** 기존 스택 + httpx(메인 의존성으로 승격), markdownify(storage XHTML→Markdown).

**Spec:** `docs/superpowers/specs/2026-08-17-llmsearch-design.md` §4 M3, §7.2, §9-10, §13. **현재 master의 코드가 기준선** (M1+M2 병합, 150 테스트 green).

## Global Constraints

- 개발·테스트는 WSL에서 동작: 모든 테스트는 API 키·실서버 없이 통과 (Fake 또는 httpx MockTransport)
- 자격증명은 `.env`에서만 읽는다 (`ATLASSIAN_PAT` / `ATLASSIAN_USER`+`ATLASSIAN_PASSWORD` / `ATLASSIAN_COOKIE`) — config.yaml·코드·로그에 평문 금지 (스펙 §7.2 P0; keyring 연동은 M3 범위 외, .env로 시작)
- 인증 폴백 순서 고정: PAT → Basic → 쿠키; 첫 등록/첫 동기화 시 자동 진단 (스펙 §7.2 P0)
- 미러 파일: `confluence/<스페이스>/<조상경로>/<제목>__<pageId>.md`, `jira/<KEY>.md` — 경로 세그먼트는 `_sanitize_segment` 재사용(LLM 아닌 원격 제목도 파일시스템 위험 문자 가능). `__<pageId>` 접미사는 동명 제목 충돌 방지(스펙 §13 레이아웃에서 유일성 위해 의도적 확장)
- 첨부파일 수집 없음 (스펙 §2 Out of Scope)
- 증분: Confluence는 version 비교, Jira는 updated 비교 — 미변경 문서는 재방출하지 않는다(재임베딩 비용 방지, M2 일정 지문과 동일 원칙)
- 삭제 전파: 트리에서 사라진 페이지·등록 해제·404 이슈 → deleted_ids + 미러 파일 제거
- 쓰기는 기존 run_sync+sync_lock 경로; 커밋은 태스크마다 conventional commits; TDD 순서 준수

## 파일 구조 (M3 추가/수정)

```
src/llmsearch/atlassian/
├─ __init__.py                  # [NEW]
├─ urls.py                      # [NEW] Confluence/Jira URL 파싱 → 등록 항목
├─ client.py                    # [NEW] AtlassianClient 프로토콜 + FakeAtlassianClient (page/issue dict 계약)
├─ htmlmd.py                    # [NEW] storage XHTML → Markdown (markdownify 래핑)
├─ auth.py                      # [NEW] .env 자격증명 후보 로딩 + 3단 폴백 진단
├─ http_client.py               # [NEW] HttpAtlassianClient (httpx, MockTransport 주입 가능)
└─ registry.py                  # [NEW] URL 등록 저장소 (data_dir/atlassian.json)
src/llmsearch/connectors/
├─ confluence.py                # [NEW] 페이지 트리 수집·증분·미러·삭제
└─ jira.py                      # [NEW] 이슈+댓글 수집·증분·미러·삭제
src/llmsearch/config.py         # [MOD] atlassian base URL 2종
src/llmsearch/web/app.py        # [MOD] SOURCES 6종 + 클라이언트 지연 진단 + 등록 API 3종
src/llmsearch/web/static/index.html  # [MOD] 소스 탭 URL 등록 폼/목록
pyproject.toml                  # [MOD] httpx 메인 의존성 승격 + markdownify
README.md                       # [MOD] M3 안내
tests/test_atlassian_urls.py, test_atlassian_client.py, test_htmlmd.py,
tests/test_atlassian_auth.py, test_confluence.py, test_jira.py,
tests/test_http_client.py, test_web_atlassian.py   # [NEW]
```

---

### Task 1: URL 파싱

**Files:**
- Create: `src/llmsearch/atlassian/__init__.py` (빈 파일), `src/llmsearch/atlassian/urls.py`
- Test: `tests/test_atlassian_urls.py`

**Interfaces:**
- Produces: `parse_atlassian_url(url: str) -> dict | None`
  - Jira: `.../browse/PROJ-123[...]` → `{"kind": "jira_issue", "key": "PROJ-123", "url": <원본>}`
  - Confluence: `...pageId=123`(viewpage.action) 또는 `/pages/123456/`(신형 경로) → `{"kind": "confluence_page", "page_id": "123", "url": <원본>}`
  - 그 외 → `None`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_atlassian_urls.py`

```python
from llmsearch.atlassian.urls import parse_atlassian_url


def test_jira_browse_url():
    r = parse_atlassian_url("https://jira.corp.com/browse/PROJ-123")
    assert r == {"kind": "jira_issue", "key": "PROJ-123",
                 "url": "https://jira.corp.com/browse/PROJ-123"}


def test_jira_browse_url_with_query():
    r = parse_atlassian_url("https://jira.corp.com/browse/ABC-9?filter=1")
    assert r["kind"] == "jira_issue" and r["key"] == "ABC-9"


def test_confluence_viewpage_pageid():
    r = parse_atlassian_url("https://wiki.corp.com/pages/viewpage.action?pageId=12345")
    assert r == {"kind": "confluence_page", "page_id": "12345",
                 "url": "https://wiki.corp.com/pages/viewpage.action?pageId=12345"}


def test_confluence_modern_path():
    r = parse_atlassian_url("https://wiki.corp.com/spaces/ENG/pages/98765/제목+문서")
    assert r["kind"] == "confluence_page" and r["page_id"] == "98765"


def test_unknown_url():
    assert parse_atlassian_url("https://example.com/whatever") is None
    assert parse_atlassian_url("not a url") is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_atlassian_urls.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/atlassian/urls.py`

```python
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_JIRA_BROWSE = re.compile(r"/browse/([A-Z][A-Z0-9]+-\d+)")
_CONF_PAGES = re.compile(r"/pages/(\d+)(?:/|$)")


def parse_atlassian_url(url: str) -> dict | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.scheme.startswith("http"):
        return None

    m = _JIRA_BROWSE.search(parsed.path)
    if m:
        return {"kind": "jira_issue", "key": m.group(1), "url": url}

    qs = parse_qs(parsed.query)
    if "pageId" in qs and qs["pageId"] and qs["pageId"][0].isdigit():
        return {"kind": "confluence_page", "page_id": qs["pageId"][0], "url": url}

    m = _CONF_PAGES.search(parsed.path)
    if m:
        return {"kind": "confluence_page", "page_id": m.group(1), "url": url}
    return None
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_atlassian_urls.py -v` → PASS (5건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/atlassian tests/test_atlassian_urls.py
git commit -m "feat: Atlassian URL 파싱(jira browse/confluence pageId·경로)"
```

---

### Task 2: AtlassianClient 프로토콜 + FakeAtlassianClient

**Files:**
- Create: `src/llmsearch/atlassian/client.py`
- Test: `tests/test_atlassian_client.py`

**Interfaces:**
- Produces (이후 태스크 전부가 이 계약에 의존):
  - 페이지 dict: `{"id": str, "space": str, "title": str, "html": str, "version": int, "updated": str(ISO), "ancestors": list[str], "url": str}`
  - 이슈 dict: `{"key": str, "summary": str, "description": str, "status": str, "assignee": str, "updated": str(ISO), "url": str, "comments": [{"author": str, "created": str, "body": str}]}`
  - `class AtlassianClient(Protocol):`
    - `def check_auth(self) -> bool` — 인증 유효성 (진단용, 예외 없이 bool)
    - `def get_page(self, page_id: str) -> dict` — 없음/권한 없음이면 `KeyError`
    - `def child_page_ids(self, page_id: str) -> list[str]`
    - `def get_issue(self, key: str) -> dict` — 없음이면 `KeyError`
  - `FakeAtlassianClient(pages: dict[str, dict] = None, children: dict[str, list[str]] = None, issues: dict[str, dict] = None, auth_ok: bool = True)`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_atlassian_client.py`

```python
import pytest
from llmsearch.atlassian.client import FakeAtlassianClient


def page(pid, title="문서", version=1):
    return {"id": pid, "space": "ENG", "title": title, "html": "<p>본문</p>",
            "version": version, "updated": "2026-08-01T10:00:00",
            "ancestors": [], "url": f"https://wiki/pages/{pid}"}


def test_get_page_and_children():
    c = FakeAtlassianClient(pages={"1": page("1"), "2": page("2", "자식")},
                            children={"1": ["2"]})
    assert c.get_page("1")["title"] == "문서"
    assert c.child_page_ids("1") == ["2"]
    assert c.child_page_ids("2") == []


def test_missing_page_raises_keyerror():
    with pytest.raises(KeyError):
        FakeAtlassianClient().get_page("999")


def test_get_issue():
    issue = {"key": "PROJ-1", "summary": "요약", "description": "설명", "status": "Open",
             "assignee": "김철수", "updated": "2026-08-02T09:00:00",
             "url": "https://jira/browse/PROJ-1",
             "comments": [{"author": "박영희", "created": "2026-08-02T10:00:00", "body": "댓글"}]}
    c = FakeAtlassianClient(issues={"PROJ-1": issue})
    assert c.get_issue("PROJ-1")["summary"] == "요약"
    with pytest.raises(KeyError):
        c.get_issue("PROJ-2")


def test_auth_flag():
    assert FakeAtlassianClient(auth_ok=False).check_auth() is False
    assert FakeAtlassianClient().check_auth() is True
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_atlassian_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/atlassian/client.py`

```python
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
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_atlassian_client.py -v` → PASS (4건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/atlassian/client.py tests/test_atlassian_client.py
git commit -m "feat: AtlassianClient 프로토콜 + FakeAtlassianClient"
```

---

### Task 3: HTML→Markdown 변환 + 의존성 추가

**Files:**
- Create: `src/llmsearch/atlassian/htmlmd.py`
- Modify: `pyproject.toml` (dependencies에 `"httpx>=0.27"`, `"markdownify>=0.13"` 추가 — httpx는 dev에서 메인으로 승격, dev 중복은 무해하므로 dev 항목은 유지)
- Test: `tests/test_htmlmd.py`

**Interfaces:**
- Produces: `html_to_markdown(html: str) -> str` — 제목/목록/표/링크 보존, script·style 제거, 3연속 이상 빈 줄 압축

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_htmlmd.py`

```python
from llmsearch.atlassian.htmlmd import html_to_markdown


def test_headings_and_lists():
    md = html_to_markdown("<h1>제목</h1><ul><li>항목1</li><li>항목2</li></ul>")
    assert "제목" in md and "항목1" in md and "항목2" in md
    assert md.count("\n\n\n") == 0  # 빈 줄 압축


def test_strips_script_and_style():
    md = html_to_markdown("<p>본문</p><script>alert(1)</script><style>.x{}</style>")
    assert "본문" in md and "alert" not in md and ".x" not in md


def test_table_preserved_as_text():
    md = html_to_markdown("<table><tr><th>이름</th></tr><tr><td>김철수</td></tr></table>")
    assert "이름" in md and "김철수" in md


def test_empty():
    assert html_to_markdown("") == ""
```

- [ ] **Step 2: 의존성 설치 + 테스트 실패 확인**

Run: `./.venv/bin/pip install -e ".[dev,vec]"` (pyproject 수정 후) → `./.venv/bin/python -m pytest tests/test_htmlmd.py -v`
Expected: FAIL — `ModuleNotFoundError: llmsearch.atlassian.htmlmd`

- [ ] **Step 3: 구현** — `src/llmsearch/atlassian/htmlmd.py`

```python
from __future__ import annotations

import re

from markdownify import markdownify

_BLANKS = re.compile(r"\n{3,}")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def html_to_markdown(html: str) -> str:
    if not html.strip():
        return ""
    cleaned = _SCRIPT_STYLE.sub("", html)
    md = markdownify(cleaned, heading_style="ATX")
    return _BLANKS.sub("\n\n", md).strip()
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_htmlmd.py -v` → PASS (4건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/atlassian/htmlmd.py pyproject.toml tests/test_htmlmd.py
git commit -m "feat: storage XHTML→Markdown 변환 + httpx/markdownify 의존성"
```

---

### Task 4: 인증 후보 로딩 + 3단 폴백 진단

**Files:**
- Create: `src/llmsearch/atlassian/auth.py`
- Test: `tests/test_atlassian_auth.py`

**Interfaces:**
- Consumes: `AtlassianClient.check_auth`
- Produces:
  - `@dataclass AtlassianAuth(mode: str, token: str = "", user: str = "", password: str = "", cookie: str = "")` — mode ∈ "pat" | "basic" | "cookie"
  - `resolve_auth_candidates(env: Mapping[str, str] | None = None) -> list[AtlassianAuth]` — env(기본 os.environ)에서 PAT → Basic → 쿠키 순서로 존재하는 후보만 (스펙 §7.2 P0 폴백 순서)
  - `diagnose(candidates: list[AtlassianAuth], make_client: Callable[[AtlassianAuth], AtlassianClient]) -> tuple[AtlassianClient, AtlassianAuth]` — 순서대로 `check_auth()` 시도, 첫 성공 반환. 전부 실패/후보 없음 → `RuntimeError`(한국어 안내: 어떤 env 변수를 설정해야 하는지)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_atlassian_auth.py`

```python
import pytest
from llmsearch.atlassian.auth import AtlassianAuth, diagnose, resolve_auth_candidates
from llmsearch.atlassian.client import FakeAtlassianClient


def test_resolve_order_and_presence():
    env = {"ATLASSIAN_PAT": "tok", "ATLASSIAN_USER": "kim", "ATLASSIAN_PASSWORD": "pw",
           "ATLASSIAN_COOKIE": "JSESSIONID=abc"}
    cands = resolve_auth_candidates(env)
    assert [c.mode for c in cands] == ["pat", "basic", "cookie"]  # 폴백 순서 고정


def test_resolve_partial():
    cands = resolve_auth_candidates({"ATLASSIAN_USER": "kim", "ATLASSIAN_PASSWORD": "pw"})
    assert [c.mode for c in cands] == ["basic"]
    assert resolve_auth_candidates({"ATLASSIAN_USER": "kim"}) == []  # password 없이는 불성립
    assert resolve_auth_candidates({}) == []


def test_diagnose_picks_first_working():
    calls = []

    def make_client(auth):
        calls.append(auth.mode)
        return FakeAtlassianClient(auth_ok=(auth.mode == "basic"))

    cands = [AtlassianAuth(mode="pat", token="t"),
             AtlassianAuth(mode="basic", user="u", password="p"),
             AtlassianAuth(mode="cookie", cookie="c")]
    client, auth = diagnose(cands, make_client)
    assert auth.mode == "basic"
    assert calls == ["pat", "basic"]  # cookie는 시도 안 함


def test_diagnose_all_fail_raises():
    with pytest.raises(RuntimeError, match="ATLASSIAN_"):
        diagnose([AtlassianAuth(mode="pat", token="t")],
                 lambda a: FakeAtlassianClient(auth_ok=False))


def test_diagnose_no_candidates_raises():
    with pytest.raises(RuntimeError, match="ATLASSIAN_"):
        diagnose([], lambda a: FakeAtlassianClient())


def test_diagnose_survives_client_construction_error():
    def make_client(auth):
        if auth.mode == "pat":
            raise ConnectionError("서버 접속 불가")
        return FakeAtlassianClient()

    client, auth = diagnose(
        [AtlassianAuth(mode="pat", token="t"), AtlassianAuth(mode="basic", user="u", password="p")],
        make_client,
    )
    assert auth.mode == "basic"  # 생성 예외도 다음 후보로 폴백
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_atlassian_auth.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/atlassian/auth.py`

```python
"""인증 3단 폴백 (스펙 §7.2 P0): PAT → Basic(사번/비밀번호) → 브라우저 세션 쿠키.

자격증명은 .env(환경변수)에서만 읽는다 — config·로그 평문 금지.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping

from .client import AtlassianClient

_HELP = (
    "Atlassian 인증 실패 — .env에 다음 중 하나를 설정하세요: "
    "ATLASSIAN_PAT(권장) / ATLASSIAN_USER+ATLASSIAN_PASSWORD / ATLASSIAN_COOKIE(브라우저 세션)"
)


@dataclass
class AtlassianAuth:
    mode: str  # "pat" | "basic" | "cookie"
    token: str = ""
    user: str = ""
    password: str = ""
    cookie: str = ""


def resolve_auth_candidates(env: Mapping[str, str] | None = None) -> list[AtlassianAuth]:
    e = os.environ if env is None else env
    out: list[AtlassianAuth] = []
    if e.get("ATLASSIAN_PAT"):
        out.append(AtlassianAuth(mode="pat", token=e["ATLASSIAN_PAT"]))
    if e.get("ATLASSIAN_USER") and e.get("ATLASSIAN_PASSWORD"):
        out.append(AtlassianAuth(mode="basic", user=e["ATLASSIAN_USER"], password=e["ATLASSIAN_PASSWORD"]))
    if e.get("ATLASSIAN_COOKIE"):
        out.append(AtlassianAuth(mode="cookie", cookie=e["ATLASSIAN_COOKIE"]))
    return out


def diagnose(
    candidates: list[AtlassianAuth],
    make_client: Callable[[AtlassianAuth], AtlassianClient],
) -> tuple[AtlassianClient, AtlassianAuth]:
    for auth in candidates:
        try:
            client = make_client(auth)
            if client.check_auth():
                return client, auth
        except Exception:  # 접속 실패·생성 오류도 다음 후보로 폴백
            continue
    raise RuntimeError(_HELP)
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_atlassian_auth.py -v` → PASS (6건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/atlassian/auth.py tests/test_atlassian_auth.py
git commit -m "feat: Atlassian 인증 3단 폴백 진단(PAT→Basic→쿠키)"
```

---

### Task 5: Confluence 커넥터 — 트리 수집·증분·미러·삭제

**Files:**
- Create: `src/llmsearch/connectors/confluence.py`
- Test: `tests/test_confluence.py`

**Interfaces:**
- Consumes: `AtlassianClient`(Task 2), `html_to_markdown`(Task 3), `summarize._sanitize_segment`, `models.Document/SyncResult`
- Produces:
  - `sync_confluence(client: AtlassianClient, page_ids: list[str], state: dict, mirror_dir: Path) -> SyncResult`
  - `state`: `{"versions": {page_id: int}, "mirrors": {page_id: str(경로)}}`
  - 시맨틱: 등록 루트별 BFS 트리 순회(중복 방문 방지, 트리당 상한 `MAX_PAGES_PER_TREE = 500`). version 동일 → 문서 미방출(미러 유지). 신규/변경 → `html_to_markdown` + 미러 파일 `mirror_dir/<살균(space)>/<살균(조상)...>/<살균(title)>__<id>.md` 기록. 이번 순회에 없는 이전 page_id → deleted_ids + 미러 삭제. 접근 불가 페이지(KeyError)는 해당 루트만 건너뛰고 계속(부분 격리)
  - Document: `source_type="confluence"`, `source_id=page_id`, `title`, `url_or_path=page["url"]`, `updated_at=_parse_dt(updated)`, `extra={"mirror_path": str, "space": space}`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_confluence.py`

```python
from pathlib import Path

from llmsearch.atlassian.client import FakeAtlassianClient
from llmsearch.connectors.confluence import sync_confluence


def page(pid, title, version=1, ancestors=None, html="<p>본문</p>"):
    return {"id": pid, "space": "ENG", "title": title, "html": html,
            "version": version, "updated": "2026-08-01T10:00:00",
            "ancestors": ancestors or [], "url": f"https://wiki/pages/{pid}"}


def make_client():
    return FakeAtlassianClient(
        pages={"1": page("1", "루트"), "2": page("2", "자식", ancestors=["루트"]),
               "3": page("3", "손자", ancestors=["루트", "자식"])},
        children={"1": ["2"], "2": ["3"]},
    )


def test_tree_sync_and_mirror(tmp_path: Path):
    r = sync_confluence(make_client(), ["1"], {}, tmp_path)
    assert {d.source_id for d in r.documents} == {"1", "2", "3"}
    d3 = next(d for d in r.documents if d.source_id == "3")
    mirror = Path(d3.extra["mirror_path"])
    assert mirror.exists()
    assert mirror.parent.name == "자식" and "손자__3" in mirror.name  # 조상 경로 + id 접미사
    assert d3.source_type == "confluence" and "본문" in d3.text


def test_unchanged_not_reemitted(tmp_path: Path):
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    r2 = sync_confluence(c, ["1"], r1.state, tmp_path)
    assert r2.documents == [] and r2.deleted_ids == []


def test_version_bump_reemitted(tmp_path: Path):
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    c.pages["2"] = page("2", "자식", version=2, ancestors=["루트"], html="<p>수정됨</p>")
    r2 = sync_confluence(c, ["1"], r1.state, tmp_path)
    assert [d.source_id for d in r2.documents] == ["2"]
    assert "수정됨" in Path(r2.documents[0].extra["mirror_path"]).read_text(encoding="utf-8")


def test_removed_page_deleted_with_mirror(tmp_path: Path):
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    mirror3 = Path(next(d for d in r1.documents if d.source_id == "3").extra["mirror_path"])
    del c.pages["3"]; c.children["2"] = []
    r2 = sync_confluence(c, ["1"], r1.state, tmp_path)
    assert r2.deleted_ids == ["3"]
    assert not mirror3.exists()


def test_inaccessible_root_isolated(tmp_path: Path):
    c = make_client()
    r = sync_confluence(c, ["999", "1"], {}, tmp_path)  # 999는 KeyError
    assert {d.source_id for d in r.documents} == {"1", "2", "3"}  # 나머지 루트는 정상


def test_filesystem_unsafe_title_sanitized(tmp_path: Path):
    c = FakeAtlassianClient(pages={"7": page("7", "제목: 위험한*이름?")})
    r = sync_confluence(c, ["7"], {}, tmp_path)
    mirror = Path(r.documents[0].extra["mirror_path"])
    assert mirror.exists()
    for ch in ':*?"<>|':
        assert ch not in mirror.name
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_confluence.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/connectors/confluence.py`

```python
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
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_confluence.py -v` → PASS (6건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/connectors/confluence.py tests/test_confluence.py
git commit -m "feat: Confluence 커넥터 — 트리 수집/version 증분/미러/삭제 정리"
```

---

### Task 6: Jira 커넥터 — 이슈+댓글·증분·미러

**Files:**
- Create: `src/llmsearch/connectors/jira.py`
- Test: `tests/test_jira.py`

**Interfaces:**
- Consumes: `AtlassianClient.get_issue`, `summarize._sanitize_segment`, `models.Document/SyncResult`
- Produces:
  - `sync_jira(client: AtlassianClient, issue_keys: list[str], state: dict, mirror_dir: Path) -> SyncResult`
  - `state`: `{"updated": {key: iso}, "mirrors": {key: 경로}}`
  - 시맨틱: 등록 키별 get_issue. updated 동일 → 미방출. 변경/신규 → Markdown(요약·상태·담당·설명·댓글) + 미러 `mirror_dir/<KEY>.md`. KeyError(삭제/권한) 또는 등록 해제 → deleted_ids + 미러 삭제
  - Document: `source_type="jira"`, `source_id=key`, `title=f"[{key}] {summary}"`, `url_or_path=issue url`, `updated_at=_parse_dt(updated)`, `extra={"mirror_path", "status", "assignee"}`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_jira.py`

```python
from pathlib import Path

from llmsearch.atlassian.client import FakeAtlassianClient
from llmsearch.connectors.jira import sync_jira


def issue(key, summary="버그 수정", updated="2026-08-02T09:00:00", comments=None):
    return {"key": key, "summary": summary, "description": "재현 절차...", "status": "Open",
            "assignee": "김철수", "updated": updated, "url": f"https://jira/browse/{key}",
            "comments": comments if comments is not None else [
                {"author": "박영희", "created": "2026-08-02T10:00:00", "body": "확인했습니다"}]}


def test_sync_and_mirror(tmp_path: Path):
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1")})
    r = sync_jira(c, ["PROJ-1"], {}, tmp_path)
    d = r.documents[0]
    assert d.source_id == "PROJ-1" and d.title == "[PROJ-1] 버그 수정"
    assert "재현 절차" in d.text and "확인했습니다" in d.text and "박영희" in d.text
    assert (tmp_path / "PROJ-1.md").exists()
    assert d.extra["status"] == "Open"


def test_unchanged_not_reemitted(tmp_path: Path):
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1")})
    r1 = sync_jira(c, ["PROJ-1"], {}, tmp_path)
    r2 = sync_jira(c, ["PROJ-1"], r1.state, tmp_path)
    assert r2.documents == [] and r2.deleted_ids == []


def test_updated_change_reemitted(tmp_path: Path):
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1")})
    r1 = sync_jira(c, ["PROJ-1"], {}, tmp_path)
    c.issues["PROJ-1"] = issue("PROJ-1", updated="2026-08-03T09:00:00",
                               comments=[{"author": "이민수", "created": "2026-08-03T09:00:00",
                                          "body": "수정 완료"}])
    r2 = sync_jira(c, ["PROJ-1"], r1.state, tmp_path)
    assert len(r2.documents) == 1 and "수정 완료" in r2.documents[0].text


def test_gone_issue_deleted_with_mirror(tmp_path: Path):
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1")})
    r1 = sync_jira(c, ["PROJ-1"], {}, tmp_path)
    c.issues = {}
    r2 = sync_jira(c, ["PROJ-1"], r1.state, tmp_path)
    assert r2.deleted_ids == ["PROJ-1"]
    assert not (tmp_path / "PROJ-1.md").exists()


def test_deregistered_key_deleted(tmp_path: Path):
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1"), "PROJ-2": issue("PROJ-2")})
    r1 = sync_jira(c, ["PROJ-1", "PROJ-2"], {}, tmp_path)
    r2 = sync_jira(c, ["PROJ-1"], r1.state, tmp_path)  # PROJ-2 등록 해제
    assert r2.deleted_ids == ["PROJ-2"]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_jira.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/connectors/jira.py`

```python
"""Jira 이슈+댓글 커넥터 (스펙 §7.2). updated 비교 증분, 미러 jira/<KEY>.md."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..atlassian.client import AtlassianClient
from ..models import Document, SyncResult
from ..summarize import _sanitize_segment


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
    updated: dict[str, str] = {}
    mirrors: dict[str, str] = {}
    documents: list[Document] = []

    for key in issue_keys:
        try:
            issue = client.get_issue(key)
        except KeyError:
            continue  # 삭제/권한 상실 — 아래 삭제 대조에서 정리
        mirror = mirror_dir / f"{_sanitize_segment(key)}.md"
        updated[key] = issue["updated"]
        mirrors[key] = str(mirror)
        if prev_updated.get(key) == issue["updated"]:
            continue
        text = _issue_markdown(issue)
        mirror_dir.mkdir(parents=True, exist_ok=True)
        mirror.write_text(text, encoding="utf-8")
        documents.append(Document(
            source_type="jira", source_id=key, title=f"[{key}] {issue['summary']}",
            text=text, url_or_path=issue["url"], updated_at=_parse_dt(issue["updated"]),
            extra={"mirror_path": str(mirror), "status": issue["status"],
                   "assignee": issue["assignee"]},
        ))

    deleted = [k for k in prev_updated if k not in updated]
    for k in deleted:
        old = prev_mirrors.get(k)
        if old and Path(old).exists():
            Path(old).unlink()

    return SyncResult(documents=documents, deleted_ids=deleted,
                      state={"updated": updated, "mirrors": mirrors})
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_jira.py -v` → PASS (5건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/connectors/jira.py tests/test_jira.py
git commit -m "feat: Jira 커넥터 — 이슈+댓글/updated 증분/미러/삭제 정리"
```

---

### Task 7: HttpAtlassianClient — 실 REST (httpx MockTransport 테스트)

**Files:**
- Create: `src/llmsearch/atlassian/http_client.py`
- Test: `tests/test_http_client.py`

**Interfaces:**
- Consumes: `AtlassianAuth`(Task 4), dict 계약(Task 2)
- Produces: `HttpAtlassianClient(confluence_base: str, jira_base: str, auth: AtlassianAuth, transport=None)` — AtlassianClient 구현. `transport`는 httpx transport 주입(테스트용 MockTransport). 404/403 → `KeyError`. Confluence Server/DC REST v1(`/rest/api/content/...`), Jira v2(`/rest/api/2/...`)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_http_client.py`

```python
import json

import httpx
import pytest
from llmsearch.atlassian.auth import AtlassianAuth
from llmsearch.atlassian.http_client import HttpAtlassianClient

CONF = "https://wiki.corp.com"
JIRA = "https://jira.corp.com"

PAGE_JSON = {
    "id": "123", "title": "설계 문서", "type": "page",
    "space": {"key": "ENG"},
    "version": {"number": 4, "when": "2026-08-01T10:00:00.000+09:00"},
    "ancestors": [{"title": "루트"}, {"title": "중간"}],
    "body": {"storage": {"value": "<p>본문</p>"}},
    "_links": {"webui": "/pages/viewpage.action?pageId=123"},
}
ISSUE_JSON = {
    "key": "PROJ-1",
    "fields": {
        "summary": "버그", "description": "설명", "updated": "2026-08-02T09:00:00.000+09:00",
        "status": {"name": "Open"}, "assignee": {"displayName": "김철수"},
        "comment": {"comments": [{"author": {"displayName": "박영희"},
                                  "created": "2026-08-02T10:00:00.000+09:00", "body": "확인"}]},
    },
}


def make_client(handler, auth=None):
    return HttpAtlassianClient(
        CONF, JIRA, auth or AtlassianAuth(mode="pat", token="tok"),
        transport=httpx.MockTransport(handler),
    )


def test_get_page_maps_contract():
    def handler(request):
        assert request.url.path == "/rest/api/content/123"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json=PAGE_JSON)

    page = make_client(handler).get_page("123")
    assert page["id"] == "123" and page["space"] == "ENG" and page["version"] == 4
    assert page["ancestors"] == ["루트", "중간"]
    assert page["html"] == "<p>본문</p>"
    assert page["url"].startswith(CONF)
    assert page["updated"].startswith("2026-08-01T10:00:00")


def test_get_page_404_keyerror():
    with pytest.raises(KeyError):
        make_client(lambda r: httpx.Response(404, json={})).get_page("9")


def test_child_page_ids_paged():
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        start = int(request.url.params.get("start", 0))
        if start == 0:
            return httpx.Response(200, json={"results": [{"id": "2"}, {"id": "3"}], "size": 2, "limit": 2})
        return httpx.Response(200, json={"results": [{"id": "4"}], "size": 1, "limit": 2})

    ids = make_client(handler).child_page_ids("1")
    assert ids == ["2", "3", "4"]
    assert len(calls) == 2  # limit만큼 찼으면 다음 페이지 요청


def test_get_issue_maps_contract():
    def handler(request):
        assert request.url.path == "/rest/api/2/issue/PROJ-1"
        return httpx.Response(200, json=ISSUE_JSON)

    issue = make_client(handler).get_issue("PROJ-1")
    assert issue["summary"] == "버그" and issue["status"] == "Open"
    assert issue["assignee"] == "김철수"
    assert issue["comments"][0]["author"] == "박영희"
    assert issue["url"] == f"{JIRA}/browse/PROJ-1"


def test_get_issue_null_fields():
    lean = {"key": "P-2", "fields": {"summary": "s", "description": None, "updated": "2026-08-01T00:00:00.000+09:00",
                                     "status": {"name": "Done"}, "assignee": None, "comment": None}}
    issue = make_client(lambda r: httpx.Response(200, json=lean)).get_issue("P-2")
    assert issue["description"] == "" and issue["assignee"] == "" and issue["comments"] == []


def test_check_auth_true_false():
    ok = make_client(lambda r: httpx.Response(200, json={"name": "kim"}))
    assert ok.check_auth() is True
    bad = make_client(lambda r: httpx.Response(401, json={}))
    assert bad.check_auth() is False


def test_check_auth_confluence_only():
    """Jira base 미설정 시 Confluence space 엔드포인트로 진단해야 한다."""
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    c = HttpAtlassianClient(CONF, "", AtlassianAuth(mode="pat", token="t"),
                            transport=httpx.MockTransport(handler))
    assert c.check_auth() is True
    assert paths == ["/rest/api/space"]


def test_auth_headers_basic_and_cookie():
    seen = {}

    def handler(request):
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"name": "x"})

    make_client(handler, AtlassianAuth(mode="cookie", cookie="JSESSIONID=abc")).check_auth()
    assert seen["cookie"] == "JSESSIONID=abc"
    make_client(handler, AtlassianAuth(mode="basic", user="u", password="p")).check_auth()
    assert seen["authorization"].startswith("Basic ")
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_http_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/atlassian/http_client.py`

```python
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
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_http_client.py -v` → PASS (8건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/atlassian/http_client.py tests/test_http_client.py
git commit -m "feat: HttpAtlassianClient — Confluence v1/Jira v2 REST(MockTransport 테스트)"
```

---

### Task 8: 레지스트리 + config 확장 + 웹 통합 + README

**Files:**
- Create: `src/llmsearch/atlassian/registry.py`
- Modify: `src/llmsearch/config.py`, `src/llmsearch/web/app.py`, `src/llmsearch/web/static/index.html`, `config.example.yaml`, `.env.example`, `README.md`
- Test: `tests/test_web_atlassian.py`, `tests/test_config.py`(추가)

**Interfaces:**
- Consumes: Task 1-7 전부, 기존 run_sync/SOURCES/sync_lock 패턴
- Produces:
  - `Registry(path: Path)`: `.add(url: str) -> dict`(파싱 실패 시 `ValueError`, 중복 시 기존 반환), `.list() -> list[dict]`, `.remove(url: str) -> bool`, `.confluence_page_ids() -> list[str]`, `.jira_keys() -> list[str]` — JSON 파일 저장, 파일 없으면 빈 목록
  - `Config`에 `confluence_base_url: str = ""`, `jira_base_url: str = ""` (yaml `atlassian: {confluence_base_url, jira_base_url}`)
  - `create_app(..., atlassian_client=None)`; `SOURCES = ("notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira")`
  - API: `POST /api/atlassian/register {"url"}` → 등록 항목 or 400, `GET /api/atlassian/registrations`, `DELETE /api/atlassian/registrations` body `{"url"}` — UI 소스 탭에 폼/목록/삭제 버튼
  - run_sync confluence/jira 분기: 클라이언트 지연 진단(`_get_atlassian_client` — resolve_auth_candidates+diagnose+HttpAtlassianClient 팩토리, base URL 미설정 시 안내 RuntimeError), 미러 디렉터리 `cfg.data_dir/"confluence"`, `cfg.data_dir/"jira"` (스펙 §13)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_config.py`에 추가:

```python
def test_atlassian_config(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "data_dir: /d\natlassian:\n  confluence_base_url: https://wiki.corp.com\n"
        "  jira_base_url: https://jira.corp.com\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.confluence_base_url == "https://wiki.corp.com"
    assert cfg.jira_base_url == "https://jira.corp.com"
    # 미설정 시 빈 문자열
    cfg_file.write_text("data_dir: /d\n", encoding="utf-8")
    assert load_config(cfg_file).confluence_base_url == ""
```

`tests/test_web_atlassian.py` (새 파일):

```python
from pathlib import Path

from fastapi.testclient import TestClient

from llmsearch.atlassian.client import FakeAtlassianClient
from llmsearch.config import Config
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.llm import FakeAnswerer
from llmsearch.summarize import FakeSummarizer
from llmsearch.web.app import create_app


def make_client(tmp_path: Path, atlassian=None) -> TestClient:
    cfg = Config(data_dir=tmp_path / "data")
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), atlassian_client=atlassian,
                     enable_scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1")


def fake_atlassian():
    return FakeAtlassianClient(
        pages={"123": {"id": "123", "space": "ENG", "title": "설계", "html": "<p>내용</p>",
                       "version": 1, "updated": "2026-08-01T10:00:00", "ancestors": [],
                       "url": "https://wiki/pages/123"}},
        issues={"PROJ-1": {"key": "PROJ-1", "summary": "버그", "description": "설명",
                           "status": "Open", "assignee": "김철수",
                           "updated": "2026-08-02T09:00:00",
                           "url": "https://jira/browse/PROJ-1", "comments": []}},
    )


def test_register_and_list_and_remove(tmp_path: Path):
    client = make_client(tmp_path)
    r = client.post("/api/atlassian/register",
                    json={"url": "https://wiki/pages/viewpage.action?pageId=123"})
    assert r.status_code == 200 and r.json()["kind"] == "confluence_page"
    r = client.post("/api/atlassian/register", json={"url": "https://jira/browse/PROJ-1"})
    assert r.json()["kind"] == "jira_issue"
    regs = client.get("/api/atlassian/registrations").json()
    assert len(regs) == 2
    assert client.post("/api/atlassian/register", json={"url": "https://x.com/a"}).status_code == 400
    r = client.request("DELETE", "/api/atlassian/registrations",
                       json={"url": "https://jira/browse/PROJ-1"})
    assert r.status_code == 200
    assert len(client.get("/api/atlassian/registrations").json()) == 1


def test_confluence_and_jira_sync(tmp_path: Path):
    client = make_client(tmp_path, atlassian=fake_atlassian())
    client.post("/api/atlassian/register",
                json={"url": "https://wiki/pages/viewpage.action?pageId=123"})
    client.post("/api/atlassian/register", json={"url": "https://jira/browse/PROJ-1"})
    assert client.post("/api/sync/confluence").json()["indexed"] == 1
    assert client.post("/api/sync/jira").json()["indexed"] == 1
    sources = {s["source"]: s for s in client.get("/api/sources").json()}
    assert sources["confluence"]["doc_count"] == 1
    assert sources["jira"]["doc_count"] == 1
    # 미러 파일 존재 (스펙 §13 레이아웃)
    assert list((tmp_path / "data" / "confluence").rglob("*.md"))
    assert (tmp_path / "data" / "jira" / "PROJ-1.md").exists()


def test_auth_failure_isolated(tmp_path: Path, monkeypatch):
    # 주입 없음 + env 자격증명 없음 → 진단 실패가 로그로 격리
    for var in ("ATLASSIAN_PAT", "ATLASSIAN_USER", "ATLASSIAN_PASSWORD", "ATLASSIAN_COOKIE"):
        monkeypatch.delenv(var, raising=False)
    client = make_client(tmp_path)
    client.post("/api/atlassian/register", json={"url": "https://jira/browse/PROJ-1"})
    r = client.post("/api/sync/jira")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "ATLASSIAN_" in r.json()["error"]
    assert client.post("/api/sync/notes").status_code == 200  # 타 소스 정상
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_web_atlassian.py tests/test_config.py -v`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'atlassian_client'` 등

- [ ] **Step 3: 구현**

`src/llmsearch/atlassian/registry.py`:

```python
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
```

`config.py` — `Config` 필드 추가:

```python
    confluence_base_url: str = ""
    jira_base_url: str = ""
```

`load_config` — `atlassian = raw.get("atlassian", {})` 한 줄 추가 후 생성자 인자에:

```python
        confluence_base_url=str(atlassian.get("confluence_base_url", "")).rstrip("/"),
        jira_base_url=str(atlassian.get("jira_base_url", "")).rstrip("/"),
```

`web/app.py` 통합 요지 (기존 구조 보존):

```python
from ..atlassian.auth import diagnose, resolve_auth_candidates
from ..atlassian.registry import Registry
from ..connectors.confluence import sync_confluence
from ..connectors.jira import sync_jira

SOURCES = ("notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira")


def _get_atlassian_client(state):
    """3단 폴백 자동 진단으로 클라이언트 지연 생성 (스펙 §7.2 P0). 진단 결과는 세션 캐시."""
    if state.get("atlassian_client") is None:
        cfg = state["config"]
        if not cfg.confluence_base_url and not cfg.jira_base_url:
            raise RuntimeError(
                "config.yaml의 atlassian.confluence_base_url / jira_base_url을 설정하세요"
            )
        from ..atlassian.http_client import HttpAtlassianClient

        def factory(auth):
            return HttpAtlassianClient(cfg.confluence_base_url, cfg.jira_base_url, auth)

        client, _auth = diagnose(resolve_auth_candidates(), factory)
        state["atlassian_client"] = client
    return state["atlassian_client"]
```

- `create_app(..., atlassian_client=None)` → `state["atlassian_client"] = atlassian_client`, `state["registry"] = Registry(config.data_dir / "atlassian.json")`
- `run_sync` 분기 추가 (기존 try/except + sync_lock 안):

```python
        elif source == "confluence":
            client = _get_atlassian_client(state)
            result = sync_confluence(client, state["registry"].confluence_page_ids(),
                                     prev, cfg.data_dir / "confluence")
        elif source == "jira":
            client = _get_atlassian_client(state)
            result = sync_jira(client, state["registry"].jira_keys(),
                               prev, cfg.data_dir / "jira")
```

- 등록 API 3종:

```python
    @app.post("/api/atlassian/register")
    def atlassian_register(payload: dict):
        try:
            return state["registry"].add(str(payload.get("url", "")))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/atlassian/registrations")
    def atlassian_registrations():
        return state["registry"].list()

    @app.delete("/api/atlassian/registrations")
    def atlassian_deregister(payload: dict):
        if not state["registry"].remove(str(payload.get("url", ""))):
            raise HTTPException(404, "등록되지 않은 URL")
        return {"ok": True}
```

`index.html` 소스 탭에 추가 (테이블 아래) — `esc()` 재사용:

```html
<h3>Confluence / Jira URL 등록</h3>
<form onsubmit="registerUrl(event)">
  <input id="atlUrl" placeholder="https://wiki.../pageId=123 또는 .../browse/PROJ-1" style="width:70%">
  <button>등록</button>
</form>
<ul id="atlList"></ul>
```

```javascript
async function loadRegistrations() {
  const regs = await (await fetch('/api/atlassian/registrations')).json();
  document.getElementById('atlList').innerHTML = regs.map(r =>
    `<li>${esc(r.kind === 'jira_issue' ? r.key : 'page ' + r.page_id)} — ` +
    `<code>${esc(r.url)}</code> ` +
    `<button onclick="removeReg(this.dataset.u)" data-u="${esc(r.url)}">삭제</button></li>`).join('');
}
async function registerUrl(e) {
  e.preventDefault();
  const url = document.getElementById('atlUrl').value.trim();
  if (!url) return;
  const r = await fetch('/api/atlassian/register', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url})});
  if (!r.ok) alert((await r.json()).detail || '등록 실패');
  document.getElementById('atlUrl').value = '';
  loadRegistrations();
}
async function removeReg(u) {
  await fetch('/api/atlassian/registrations', {method: 'DELETE',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url: u})});
  loadRegistrations();
}
```

`show('sources')` 분기에 `loadRegistrations();` 호출 추가.

`config.example.yaml`에 추가:

```yaml
atlassian:
  confluence_base_url: ""   # 예: https://wiki.corp.com (사내 Server/DC)
  jira_base_url: ""         # 예: https://jira.corp.com
```

`.env.example`에 추가:

```
# Atlassian 인증 — 아래 중 하나 (폴백 순서: PAT → USER/PASSWORD → COOKIE)
ATLASSIAN_PAT=
ATLASSIAN_USER=
ATLASSIAN_PASSWORD=
ATLASSIAN_COOKIE=
```

`README.md`에 M3 절 추가:

```markdown
## Confluence / Jira 연동 (M3)
1. `config.yaml`의 `atlassian:` base URL 2종 설정, `.env`에 인증(PAT 권장 — DC 7.9+; 안 되면 사번/비밀번호, 최후엔 브라우저 쿠키)
2. 소스 탭 하단 폼에 Confluence 페이지 URL(하위 트리 포함 수집) 또는 Jira 이슈 URL 등록
3. confluence / jira 동기화 — 미러는 `data_dir/confluence/`, `data_dir/jira/`에 Markdown으로 저장됨
- 인증 진단은 첫 동기화 때 PAT→Basic→쿠키 순서로 자동 시도, 실패 시 로그 탭에 안내
```

- [ ] **Step 4: 전체 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/ -q` → 전부 PASS (기존 150 + 신규)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/atlassian/registry.py src/llmsearch/config.py src/llmsearch/web tests/test_web_atlassian.py tests/test_config.py config.example.yaml .env.example README.md
git commit -m "feat: Confluence/Jira 웹 통합 — 등록 API/폼, 인증 진단, 미러 디렉터리"
```

---

## M3 완료 기준 (수동 검증 체크리스트 — 사내망)

1. `.env`에 PAT(또는 사번/비밀번호) 설정 → 소스 탭에서 실제 Confluence 페이지 URL 등록 → 동기화 → `data_dir/confluence/` 미러 구조 확인
2. 실제 Jira 이슈 URL 등록 → 동기화 → 댓글 포함 여부 확인
3. 인증 실패 시나리오(.env 비움) → 로그 탭 안내 확인, 타 소스 정상
4. 채팅에서 "OO 설계 문서 내용" 질문 → confluence 출처 카드, "PROJ-123 진행 상황" → jira 출처 카드
5. Confluence에서 페이지 이동/삭제 후 재동기화 → 미러·인덱스 정리 확인
```
