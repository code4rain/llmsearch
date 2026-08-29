# llmsearch M6a — 운영 완성 (설정·재요약·사용량) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스펙 M6a — 선행 리팩터(DB 커넥션 state 조회·가드·스케줄러 격리), 상태 변경 API 로컬 오리진 검사, 설정 탭 rules.md 편집(즉시 반영·요약 규칙 주입·notes 인덱싱), 재요약(문서별/전체), 사용량 GUI 표시, E2E 확장.

**Architecture:** 웹 계층(`web/app.py`)은 DB 커넥션을 클로저가 아닌 `state`에서 호출 시점에 조회한다(M6b 스키마 불일치 복구의 전제). 재요약은 local_docs 동기화 상태의 항목을 **센티널 `[0.0, 0]`로 치환**해 다음 `run_sync`가 강제 재요약하게 한다 — 항목을 제거하면 `prior_map`이 소실되어 요약 md가 중복 생성되므로 절대 제거하지 않는다. 답변기 규칙은 `Answerer.update_rules`로 재시작 없이 반영. 요약 규칙(`## 요약 규칙`)은 새 `summary_rules` 인자로 요약 프롬프트에 주입된다.

**Tech Stack:** Python 3.12, FastAPI, SQLite, 표준 라이브러리만 (신규 의존성 없음), Playwright E2E

**Spec:** `docs/superpowers/specs/2026-08-29-llmsearch-m6-design.md` (§1 M6a 열, §2, §3, §4, §5, §8)

**Ruling (계획 시점):** 스펙 §2의 "Content-Type: application/json 요구(415)"는 채택하지 않는다. 브라우저는 크로스오리진 POST(no-cors 단순 요청 포함)에 항상 `Origin`을 보내므로 Origin/Referer 검사만으로 CSRF가 차단되고, JSON 요구는 바디 없는 `/api/sync/{source}` 기존 테스트·E2E를 깨뜨린다. 스펙 §2를 이에 맞춰 수정했다.

## Global Constraints

- 상한은 요약·인덱싱 경로에만 적용, 검색·답변은 유지 (상위 스펙 §10) — 재요약은 `run_sync` 게이트를 그대로 통과한다
- 재요약은 상태 항목 **제거 금지, 센티널 `[0.0, 0]` 치환** (스펙 §4 — `prior_map` 유지가 요약 md 중복 생성을 막는다)
- UI 동적 값은 `esc()` 또는 `textContent`/`.value`로만 주입 (CLAUDE.md 보안) — rules 본문을 innerHTML에 넣지 않는다
- 웹 테스트 `TestClient(app, base_url="http://127.0.0.1")` 필수 (TrustedHostMiddleware)
- Python 4칸 들여쓰기, 표준 라이브러리만, 기존 테스트 271개 무변경 통과(의도된 계약 변경 없음), 전체 `./.venv/bin/pytest -q` 태스크마다 green
- E2E 기존 45건 무변경, 신규 시나리오는 verify.py **9단계와 10단계 사이**에 삽입 (10단계가 상한을 소진하므로 그 뒤에서는 성립하지 않음)
- 커밋 메시지 한국어, `feat:`/`refactor:`/`test:` 접두사

---

### Task 1: 선행 리팩터 — 커넥션 state 조회·`_require_db`·run_sync 가드·스케줄러 예외 격리

**Files:**
- Modify: `src/llmsearch/web/app.py` (run_sync 124-200, create_app 233-400)
- Test: `tests/test_web.py` (추가)

**Interfaces:**
- Consumes: 기존 `state["conn"]`/`state["read_conn"]`
- Produces: `run_sync`는 `state["conn"] is None`이면 예외 없이 `entry["error"]` 반환; `create_app` 내부 `_require_db()` (없으면 `HTTPException(503)`); 엔드포인트·`search_fn`이 `state["read_conn"]`을 호출 시점에 조회. Task 6/7과 M6b가 `_require_db`를 사용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py` 끝에 추가:

```python
def test_run_sync_without_db_returns_error_entry(tmp_path: Path):
    """M6a 선행 리팩터: conn이 None(스키마 불일치 등)이면 예외 대신 error entry — 스케줄러 보호."""
    from llmsearch.web.app import run_sync

    client = make_app(tmp_path)
    state = client.app.state.llmsearch
    state["conn"] = None
    state["schema_mismatch"] = "index.db schema v0 != v1"
    entry = run_sync(state, "notes")
    assert entry["ok"] is False and "schema" in entry["error"]
    assert state["log"][0] is entry


def test_db_endpoints_503_without_read_conn(tmp_path: Path):
    client = make_app(tmp_path)
    state = client.app.state.llmsearch
    state["read_conn"] = None
    state["schema_mismatch"] = "index.db schema v0 != v1"
    assert client.post("/api/chat", json={"question": "q", "history": []}).status_code == 503
    assert client.get("/api/para/projects").status_code == 503
    assert client.post("/api/open", json={"url_or_path": "x"}).status_code == 503
    r = client.get("/api/sources")
    assert r.status_code == 200
    assert all(s["doc_count"] == 0 for s in r.json())
    assert r.json()[0]["schema_mismatch"] == "index.db schema v0 != v1"


def test_read_conn_is_looked_up_at_call_time(tmp_path: Path):
    """커넥션을 클로저가 아니라 state에서 조회해야 M6b가 재구축 후 교체할 수 있다."""
    from llmsearch import db

    client = make_app(tmp_path)
    state = client.app.state.llmsearch
    client.post("/api/sync/notes")
    fresh = db.open_db(state["config"].db_path)
    state["read_conn"].close()
    state["read_conn"] = fresh
    r = client.get("/api/sources")
    assert next(s for s in r.json() if s["source"] == "notes")["doc_count"] == 1
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v -k "without_db or without_read_conn or call_time"`
Expected: FAIL — `run_sync`가 `AttributeError: 'NoneType' object has no attribute 'execute'`, 엔드포인트가 500, 마지막 테스트는 닫힌 커넥션으로 `ProgrammingError`

- [ ] **Step 3: 구현**

`src/llmsearch/web/app.py` `run_sync` — `conn = state["conn"]`(127행)을 지우고 `with state["sync_lock"]:` 직후에 가드와 함께 둔다:

```python
    with state["sync_lock"]:  # 단일 sqlite3.Connection 공유 쓰기 직렬화 (스펙 §5 P0)
        conn = state["conn"]  # 락 안에서 획득 — M6b 재구축이 커넥션을 교체해도 낡은 참조를 들지 않는다
        if conn is None:
            # 스키마 불일치 등으로 DB를 열지 못한 상태 — 예외를 던지면 scheduler_loop가 죽는다
            entry["ok"] = False
            entry["error"] = state.get("schema_mismatch") or "index.db를 열 수 없습니다 — 재구축이 필요합니다"
            state["log"].insert(0, entry)
            del state["log"][200:]
            return entry
        tracker: UsageTracker = state["usage"]
```

`create_app`에서 `read_conn`/`conn` 클로저 캡처를 전부 `state[...]` 조회로 바꾸고 가드 헬퍼를 추가한다. `state = {...}` 정의 직후:

```python
    def _require_db() -> None:
        """DB를 만지는 엔드포인트 진입 가드 — 스키마 불일치 상태에서는 503으로 안내 (M6b 배너와 짝)."""
        if state["read_conn"] is None or state["conn"] is None:
            raise HTTPException(503, state.get("schema_mismatch") or "index.db를 열 수 없습니다 — 재구축이 필요합니다")
```

`scheduler_loop`:

```python
    async def scheduler_loop():
        while True:
            await asyncio.sleep(config.sync_interval_minutes * 60)
            for source in _scheduled_sources(state):
                try:
                    await asyncio.to_thread(run_sync, state, source)
                except Exception:  # run_sync는 내부에서 격리하지만, 어떤 예외에도 루프는 살아야 한다
                    _logger.exception("스케줄러 동기화 예외 격리: %s", source)
```

`/api/sources`:

```python
    @app.get("/api/sources")
    def sources():
        read_conn = state["read_conn"]
        out = []
        for source in SOURCES:
            last = next((e for e in state["log"] if e["source"] == source), None)
            entry = {"source": source, "doc_count": 0,
                     "last_sync": last["at"] if last else None,
                     "last_error": last["error"] if last else None}
            if read_conn is None:
                entry["schema_mismatch"] = state.get("schema_mismatch") or "index.db를 열 수 없습니다"
            else:
                row = read_conn.execute("SELECT COUNT(*) FROM documents WHERE source_type=?", (source,)).fetchone()
                entry["doc_count"] = row[0]
                if source == "outlook_mail":
                    entry["backlog"] = backlog_hint(indexer.get_sync_state(read_conn, source))
            out.append(entry)
        return out
```

`/api/sync/{source}`: 본문 첫 줄에 `_require_db()` 추가. `/api/para/projects`: 첫 줄 `_require_db()`, `read_conn.execute` → `state["read_conn"].execute`. `/api/archive`: `with` 앞에 `_require_db()`, `archive_project(conn, ...)` → `archive_project(state["conn"], ...)`. `/api/open`: `try:` 앞에 `_require_db()`, 세 곳의 `read_conn.execute` → `state["read_conn"].execute`. `/api/chat`: 첫 줄 `_require_db()`(`record("answer")`보다 앞 — 503인 요청은 카운트하지 않는다), `search_fn`:

```python
        def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
            return search.search(state["read_conn"], embedder, query, source_filter=source_filter,
                                 date_from=date_from, date_to=date_to, sender=sender)
```

마지막으로 `conn = db.open_db(config.db_path)` / `read_conn = db.open_db(config.db_path)` 두 지역변수는 `state` 구성에만 쓰이므로 그대로 두되, 이후 본문에서 `read_conn`·`conn` 이름을 직접 참조하는 곳이 남지 않았는지 `grep -n "read_conn\.\|(conn," src/llmsearch/web/app.py`로 확인한다(허용: `state = {... "conn": conn, "read_conn": read_conn ...}` 한 곳).

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` → 271 + 3 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/app.py tests/test_web.py
git commit -m "refactor: 웹 계층 DB 커넥션 state 조회·_require_db 가드·run_sync None 가드·스케줄러 예외 격리 (M6a 선행)"
```

---

### Task 2: 상태 변경 API 로컬 오리진 검사

**Files:**
- Modify: `src/llmsearch/web/app.py`, `src/llmsearch/web/static/index.html` (`syncNow`, `removeReg`)
- Test: `tests/test_web.py` (추가)

**Interfaces:**
- Produces: 모듈 함수 `local_origin_only(request: Request) -> None` — FastAPI 의존성. Task 3/6이 `dependencies=[Depends(local_origin_only)]`로 재사용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_mutating_endpoints_reject_foreign_origin(tmp_path: Path):
    """스펙 M6 §2: 임의 웹페이지의 CSRF(no-cors POST)로 동기화·아카이브·등록이 트리거되면 안 된다."""
    client = make_app(tmp_path)
    evil = {"Origin": "http://evil.example"}
    assert client.post("/api/sync/notes", headers=evil).status_code == 403
    assert client.post("/api/archive", json={"project": "x"}, headers=evil).status_code == 403
    assert client.post("/api/atlassian/register", json={"url": "x"}, headers=evil).status_code == 403
    assert client.request("DELETE", "/api/atlassian/registrations", json={"url": "x"}, headers=evil).status_code == 403
    assert client.post("/api/sync/notes", headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/sync/notes", headers={"Referer": "https://evil.example/page"}).status_code == 403


def test_mutating_endpoints_accept_local_origin_or_no_origin(tmp_path: Path):
    client = make_app(tmp_path)
    assert client.post("/api/sync/notes").status_code == 200  # curl/CLI — Origin 없음
    assert client.post("/api/sync/notes", headers={"Origin": "http://127.0.0.1:8642"}).status_code == 200
    assert client.post("/api/sync/notes", headers={"Origin": "http://localhost:8642"}).status_code == 200
    assert client.post("/api/sync/notes", headers={"Referer": "http://127.0.0.1:8642/"}).status_code == 200
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v -k origin`
Expected: FAIL — 403이어야 할 요청이 200

- [ ] **Step 3: 구현**

`app.py` import에 `from urllib.parse import urlsplit`, `from fastapi import Depends, FastAPI, HTTPException, Request`. `_logger` 정의 아래에 추가:

```python
def _is_local_origin(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")


def local_origin_only(request: Request) -> None:
    """상태 변경 API의 CSRF 방어 (스펙 M6 §2).

    브라우저는 크로스오리진 POST/PUT/DELETE(no-cors 단순 요청 포함)에 항상 Origin을 붙이므로,
    Origin(없으면 Referer)이 로컬이 아니면 거부한다. 헤더가 둘 다 없는 요청(curl·CLI·TestClient)은
    브라우저가 아니므로 통과. "null" Origin(샌드박스·file://)도 거부된다.
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin and not _is_local_origin(origin):
        raise HTTPException(403, "로컬 브라우저(127.0.0.1)에서만 호출할 수 있습니다")
```

적용 — 데코레이터에 `dependencies=[Depends(local_origin_only)]` 추가: `@app.post("/api/sync/{source}", ...)`, `@app.post("/api/atlassian/register", ...)`, `@app.delete("/api/atlassian/registrations", ...)`, `@app.post("/api/archive", ...)`, `@app.post("/api/open", ...)`, `@app.post("/api/chat", ...)`.

`index.html`: `syncNow`·`removeReg`의 `fetch`는 같은 오리진이라 브라우저가 Origin을 자동으로 붙인다 — 변경 불필요. 단 확인 차원에서 `removeReg`가 이미 JSON 헤더를 보내는지 유지.

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest -q` → 전체 green (기존 웹 테스트는 Origin 헤더를 보내지 않으므로 무변경 통과)

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/app.py tests/test_web.py
git commit -m "feat: 상태 변경 API 로컬 오리진 검사 — CSRF로 동기화·아카이브·등록 트리거 차단 (스펙 M6 §2)"
```

---

### Task 3: rules.md 편집 API + 답변기 즉시 반영 + 설정 탭 UI

**Files:**
- Modify: `src/llmsearch/rules.py`, `src/llmsearch/llm.py`, `src/llmsearch/web/app.py`, `src/llmsearch/web/static/index.html`
- Test: `tests/test_rules.py`, `tests/test_llm.py`, `tests/test_web.py` (추가)

**Interfaces:**
- Produces: `rules.parse_rules_md(text: str) -> dict[str, str]` (`load_rules_md`는 이를 호출); `Answerer.update_rules(sections: dict[str, str]) -> None` (Fake는 `self.rules`에 보관); `GET /api/rules` → `{"text", "path", "sections"}`, `PUT /api/rules {"text"}` → `{"ok", "sections"}`; 상수 `RULES_TEMPLATE`; UI 요소 `#rulesText`, `#saveRulesBtn`, `#rulesStatus`, `#rulesPath`, nav 버튼 "설정", `<div id="settings" class="tab">`. Task 6이 설정 탭에 버튼을 추가하고, Task 8 E2E가 이 id들을 사용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_rules.py` 끝에 추가:

```python
def test_parse_rules_md_matches_load(tmp_path):
    from llmsearch.rules import load_rules_md, parse_rules_md

    text = "# 규칙\n\n## 용어집\nPJA = 프로젝트A\n\n## 답변 규칙\n두괄식\n"
    p = tmp_path / "rules.md"
    p.write_text(text, encoding="utf-8")
    assert parse_rules_md(text) == load_rules_md(p) == {"용어집": "PJA = 프로젝트A", "답변 규칙": "두괄식"}
```

`tests/test_llm.py` 끝에 추가:

```python
def test_fake_answerer_update_rules_keeps_last():
    from llmsearch.llm import FakeAnswerer

    a = FakeAnswerer()
    assert a.rules == {}
    a.update_rules({"답변 규칙": "두괄식", "용어집": "PJA = 프로젝트A"})
    assert a.rules["답변 규칙"] == "두괄식"


def test_claude_answerer_update_rules_changes_system_prompt(monkeypatch):
    import sys, types
    from llmsearch.llm import ClaudeAnswerer

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: object()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    a = ClaudeAnswerer(answer_rules="", glossary="")
    assert "## 답변 규칙" not in a._system()
    a.update_rules({"답변 규칙": "두괄식", "용어집": "PJA = 프로젝트A"})
    assert "## 답변 규칙\n두괄식" in a._system() and "## 용어집\nPJA = 프로젝트A" in a._system()
    a.update_rules({})
    assert "## 답변 규칙" not in a._system()
```

`tests/test_web.py` 끝에 추가:

```python
def test_rules_get_template_when_missing(tmp_path: Path):
    client = make_app(tmp_path)
    r = client.get("/api/rules")
    assert r.status_code == 200
    body = r.json()
    assert body["text"].startswith("# 규칙 (rules.md)")
    assert body["sections"] == ["용어집", "분류 규칙", "요약 규칙", "답변 규칙"]
    assert body["path"].endswith("rules.md")


def test_rules_put_saves_and_updates_answerer(tmp_path: Path):
    client = make_app(tmp_path)
    text = "# 규칙 (rules.md)\n\n## 용어집\nPJA = 프로젝트A\n\n## 답변 규칙\n두괄식\n"
    r = client.put("/api/rules", json={"text": text})
    assert r.status_code == 200 and r.json() == {"ok": True, "sections": ["용어집", "답변 규칙"]}
    path = client.app.state.llmsearch["config"].rules_md_path
    assert path.read_text(encoding="utf-8") == text
    assert not path.with_name(path.name + ".tmp").exists()  # 원자적 저장 — tmp 잔재 없음
    assert client.app.state.llmsearch["answerer"].rules["답변 규칙"] == "두괄식"
    assert client.get("/api/rules").json()["text"] == text


def test_rules_put_rejects_bad_input(tmp_path: Path):
    client = make_app(tmp_path)
    assert client.put("/api/rules", json={"text": 123}).status_code == 400
    assert client.put("/api/rules", json={}).status_code == 400
    big = "가" * (90 * 1024)  # UTF-8 3바이트 × 90K = 270KB > 256KB
    assert client.put("/api/rules", json={"text": big}).status_code == 400
    assert not client.app.state.llmsearch["config"].rules_md_path.exists()  # 파일 미변경
    assert client.put("/api/rules", json={"text": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_settings_tab_in_index(tmp_path: Path):
    client = make_app(tmp_path)
    html = client.get("/").text
    assert 'id="settings"' in html and 'id="rulesText"' in html and "설정" in html
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_rules.py tests/test_llm.py tests/test_web.py -v -k "rules or settings or update_rules"`
Expected: FAIL — `ImportError: parse_rules_md`, `AttributeError: rules/update_rules`, `/api/rules` 404

- [ ] **Step 3: 구현**

`src/llmsearch/rules.py` — `load_rules_md`를 파서 분리:

```python
def parse_rules_md(text: str) -> dict[str, str]:
    """`## 섹션` 헤더 단위로 본문을 나눈다 — GUI가 저장 전 본문으로 섹션 목록을 보여줄 때도 같은 파서."""
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def load_rules_md(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_rules_md(path.read_text(encoding="utf-8"))
```

`src/llmsearch/llm.py`:

```python
class Answerer(Protocol):
    def answer_stream(self, question: str, history: list[dict], search_fn: SearchFn) -> Iterator[dict]: ...

    def update_rules(self, sections: dict[str, str]) -> None: ...


class FakeAnswerer:
    def __init__(self):
        self.rules: dict[str, str] = {}  # 마지막 update_rules 값 — 테스트 관찰용

    def update_rules(self, sections: dict[str, str]) -> None:
        self.rules = dict(sections)

    def answer_stream(self, question, history, search_fn) -> Iterator[dict]:
        ...(기존 본문 그대로)
```

`ClaudeAnswerer`에 메서드 추가 (`_system` 위):

```python
    def update_rules(self, sections: dict[str, str]) -> None:
        """설정 탭 저장 즉시 반영 — 재시작 없이 다음 답변부터 새 규칙 사용 (스펙 M6 §3)."""
        self.answer_rules = sections.get("답변 규칙", "")
        self.glossary = sections.get("용어집", "")
```

`src/llmsearch/web/app.py` — import에 `import os`, `from ..rules import load_rules_md, parse_rules_md`. 모듈 상수:

```python
RULES_TEMPLATE = "# 규칙 (rules.md)\n\n## 용어집\n\n## 분류 규칙\n\n## 요약 규칙\n\n## 답변 규칙\n"
_RULES_MAX_BYTES = 256 * 1024  # 파일 크기 방어 — 본문은 이미 파싱된 뒤라 메모리 보호 목적은 아님
```

엔드포인트 (`/api/log` 뒤):

```python
    @app.get("/api/rules")
    def rules_get():
        path = config.rules_md_path
        text = path.read_text(encoding="utf-8") if path.exists() else RULES_TEMPLATE
        return {"text": text, "path": str(path), "sections": list(parse_rules_md(text))}

    @app.put("/api/rules", dependencies=[Depends(local_origin_only)])
    def rules_put(payload: dict):
        text = payload.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, "text는 문자열이어야 합니다")
        data = text.encode("utf-8")
        if len(data) > _RULES_MAX_BYTES:
            raise HTTPException(400, f"rules.md는 {_RULES_MAX_BYTES // 1024}KB 이하여야 합니다")
        path = config.rules_md_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # 원자적 교체 — 저장 중 크래시로 규칙 파일이 절단되지 않게
        sections = parse_rules_md(text)
        state["answerer"].update_rules(sections)  # 동기화 경로는 run_sync마다 파일을 다시 읽는다
        return {"ok": True, "sections": list(sections)}
```

`index.html` — nav에 `<button onclick="show('settings')">설정</button>` 추가(로그 버튼 뒤). 로그 탭 div 뒤에:

```html
<div id="settings" class="tab">
  <h3>규칙 (rules.md) <small id="rulesPath"></small></h3>
  <p><small>섹션 헤더가 주입 위치를 정한다: ## 용어집(모든 LLM 호출) · ## 분류 규칙 · ## 요약 규칙 · ## 답변 규칙. 규칙 변경은 신규 문서부터 적용 — 기존 문서는 아래 재요약으로.</small></p>
  <textarea id="rulesText" style="width:100%;height:60vh;font-family:monospace"></textarea><br>
  <button id="saveRulesBtn" onclick="saveRules()">저장</button> <span id="rulesStatus"></span>
  <h3>운영</h3>
  <div id="opsButtons"></div>
</div>
```

`show()`에 `if (id === 'settings') loadRules();` 추가. JS:

```js
async function loadRules() {
  const r = await (await fetch('/api/rules')).json();
  document.getElementById('rulesText').value = r.text;  // .value — innerHTML 금지 (</textarea> XSS)
  document.getElementById('rulesPath').textContent = r.path;
  document.getElementById('rulesStatus').textContent = '섹션: ' + (r.sections.join(', ') || '(없음)');
}
async function saveRules() {
  const r = await fetch('/api/rules', {method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: document.getElementById('rulesText').value})});
  const data = await r.json();
  document.getElementById('rulesStatus').textContent =
    r.ok ? '저장됨 · 섹션: ' + (data.sections.join(', ') || '(없음)') : (data.detail || '저장 실패');
}
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_rules.py tests/test_llm.py tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/rules.py src/llmsearch/llm.py src/llmsearch/web/app.py src/llmsearch/web/static/index.html tests/test_rules.py tests/test_llm.py tests/test_web.py
git commit -m "feat: 설정 탭 rules.md 편집 — GET/PUT /api/rules 원자적 저장·답변기 즉시 반영 (스펙 M6 §3)"
```

---

### Task 4: `## 요약 규칙` 주입 — summarize_and_classify `summary_rules` 인자 체인

**Files:**
- Modify: `src/llmsearch/summarize.py`, `src/llmsearch/connectors/local_docs.py`, `src/llmsearch/web/app.py` (run_sync local_docs 분기)
- Test: `tests/test_summarize.py`, `tests/test_local_docs.py` (추가)

**Interfaces:**
- Consumes: Task 3의 rules 저장(즉시 반영은 run_sync가 파일을 다시 읽어 자동)
- Produces: `summarize.build_summary_prompt(title, text, projects, areas, existing_resources, prior_category, glossary, rules, summary_rules="") -> str`; `Summarizer.summarize_and_classify(..., rules: str, summary_rules: str = "")`; `sync_local_docs(..., summary_rules: str = "")`. `CountingSummarizer`는 `*args, **kwargs` 위임이라 무변경.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_summarize.py` 끝에 추가:

```python
def test_build_summary_prompt_injects_summary_rules():
    from llmsearch.summarize import build_summary_prompt

    p = build_summary_prompt(title="보고서", text="본문", projects=["프로젝트A"], areas=[],
                             existing_resources=[], prior_category=None, glossary="PJA = 프로젝트A",
                             rules="경쟁사 자료는 Resources/경쟁사", summary_rules="실적 수치는 표로 보존")
    assert "## 용어집\nPJA = 프로젝트A" in p
    assert "## 분류 규칙\n경쟁사 자료는 Resources/경쟁사" in p
    assert "## 요약 규칙\n실적 수치는 표로 보존" in p
    assert p.index("## 분류 규칙") < p.index("## 요약 규칙") < p.index("--- 문서 제목")
    assert "## 요약 규칙" not in build_summary_prompt("t", "x", [], [], [], None, "", "")


def test_fake_summarizer_accepts_summary_rules():
    from llmsearch.summarize import FakeSummarizer

    r = FakeSummarizer().summarize_and_classify(
        title="문서", text="프로젝트A", projects=["프로젝트A"], areas=[], existing_resources=[],
        prior_category=None, glossary="", rules="", summary_rules="표로")
    assert r.category == "Projects/프로젝트A"
```

`tests/test_local_docs.py` 끝에 추가:

```python
def test_summary_rules_passed_to_summarizer(tmp_path: Path, patch_extract):
    class Recording(FakeSummarizer):
        def __init__(self):
            self.kwargs = None

        def summarize_and_classify(self, *args, **kwargs):
            self.kwargs = kwargs
            return super().summarize_and_classify(*args, **kwargs)

    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "a.pptx").write_bytes(b"x")
    s = Recording()
    local_docs.sync_local_docs(
        folders=[docs], excludes=[], overrides=[], summarizer=s, summaries_dir=tmp_path / "summaries",
        projects=["프로젝트A"], areas=[], glossary="", class_rules="", state={}, prior_map={},
        summary_rules="실적 수치는 표로",
    )
    assert s.kwargs["summary_rules"] == "실적 수치는 표로"
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_summarize.py tests/test_local_docs.py -v -k summary_rules`
Expected: FAIL — `ImportError: build_summary_prompt`, `TypeError: unexpected keyword 'summary_rules'`

- [ ] **Step 3: 구현**

`src/llmsearch/summarize.py` — Protocol 시그니처 끝에 `summary_rules: str = ""` 추가; `FakeSummarizer.summarize_and_classify(self, title, text, projects, areas, existing_resources, prior_category, glossary, rules, summary_rules="")`(본문 무변경). `_SUMMARY_PROMPT`의 `{rules}` 다음 줄에 `{summary_rules}` 추가:

```
{glossary}
{rules}
{summary_rules}

--- 문서 제목: {title} ---
```

모듈 함수 추가(`_SUMMARY_PROMPT` 아래):

```python
def build_summary_prompt(title, text, projects, areas, existing_resources, prior_category,
                         glossary, rules, summary_rules=""):
    """요약·분류 프롬프트 조립 — 규칙 섹션 주입 위치를 테스트로 고정하기 위해 분리 (스펙 §9 표)."""
    return _SUMMARY_PROMPT.format(
        projects=", ".join(projects) or "(없음)",
        areas=", ".join(areas) or "(없음)",
        resources=", ".join(existing_resources) or "(없음)",
        prior=f"- 이 문서의 기존 분류: {prior_category} (특별한 이유 없으면 유지)" if prior_category else "",
        glossary=f"\n## 용어집\n{glossary}" if glossary else "",
        rules=f"\n## 분류 규칙\n{rules}" if rules else "",
        summary_rules=f"\n## 요약 규칙\n{summary_rules}" if summary_rules else "",
        title=title,
        text=text[:MAX_SUMMARY_INPUT_CHARS],
    )
```

`GeminiSummarizer.summarize_and_classify(self, title, text, projects, areas, existing_resources, prior_category, glossary, rules, summary_rules="")` — 본문의 `prompt = _SUMMARY_PROMPT.format(...)` 전체를 `prompt = build_summary_prompt(title, text, projects, areas, existing_resources, prior_category, glossary, rules, summary_rules)`로 교체.

`src/llmsearch/connectors/local_docs.py` — `sync_local_docs` 시그니처 `renderer` 뒤에 `summary_rules: str = ""` 추가; `summarizer.summarize_and_classify(...)` 호출의 `glossary=glossary, rules=class_rules,` 뒤에 `summary_rules=summary_rules,` 추가.

`src/llmsearch/web/app.py` `run_sync` local_docs 분기 — `glossary=rules_md.get("용어집", ""), class_rules=rules_md.get("분류 규칙", ""),` 뒤에 `summary_rules=rules_md.get("요약 규칙", ""),` 추가.

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_summarize.py tests/test_local_docs.py tests/test_usage.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/summarize.py src/llmsearch/connectors/local_docs.py src/llmsearch/web/app.py tests/test_summarize.py tests/test_local_docs.py
git commit -m "feat: rules.md '## 요약 규칙'을 요약 프롬프트에 주입 — summary_rules 인자 체인 (스펙 §9 표)"
```

---

### Task 5: notes 커넥터 `extra_files` — rules.md 인덱싱

**Files:**
- Modify: `src/llmsearch/connectors/notes.py`, `src/llmsearch/web/app.py` (run_sync notes 분기)
- Test: `tests/test_notes.py`, `tests/test_web.py` (추가)

**Interfaces:**
- Produces: `sync_notes(folders, excludes, state, extra_files: Sequence[Path] = ()) -> SyncResult`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_notes.py` 끝에 추가:

```python
def test_extra_files_indexed_and_deduped(tmp_path: Path):
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("# 메모A", encoding="utf-8")
    rules = tmp_path / "data" / "rules.md"
    rules.parent.mkdir()
    rules.write_text("# 규칙 (rules.md)\n\n## 용어집\nPJA = 프로젝트A\n", encoding="utf-8")
    result = sync_notes([notes], [], {}, extra_files=[rules, notes / "a.md", tmp_path / "없음.md"])
    ids = [d.source_id for d in result.documents]
    assert len(ids) == 2 and str(rules.resolve()) in ids  # 존재하는 extra만, 폴더 안 파일은 중복 없이
    assert next(d for d in result.documents if d.source_id == str(rules.resolve())).title == "규칙 (rules.md)"
    assert str(rules.resolve()) in result.state["files"]


def test_extra_file_respects_exclude_and_delete(tmp_path: Path):
    rules = tmp_path / "rules.md"
    rules.write_text("# 규칙", encoding="utf-8")
    r1 = sync_notes([], [], {}, extra_files=[rules])
    assert len(r1.documents) == 1
    assert sync_notes([], ["path:*rules.md"], {}, extra_files=[rules]).documents == []
    rules.unlink()
    r2 = sync_notes([], [], r1.state, extra_files=[rules])
    assert r2.deleted_ids == [str(rules.resolve())]
```

`tests/test_web.py` 끝에 추가:

```python
def test_rules_md_indexed_as_notes(tmp_path: Path):
    """스펙 §9: rules.md는 notes로 취급되어 인덱싱된다 — '내가 정한 규칙'도 검색 가능."""
    client = make_app(tmp_path)
    client.put("/api/rules", json={"text": "# 규칙 (rules.md)\n\n## 용어집\nPJA = 프로젝트A\n"})
    assert client.post("/api/sync/notes").json()["indexed"] == 2  # kick.md + rules.md
    read_conn = client.app.state.llmsearch["read_conn"]
    titles = {r[0] for r in read_conn.execute("SELECT title FROM documents WHERE source_type='notes'")}
    assert "규칙 (rules.md)" in titles
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_notes.py tests/test_web.py -v -k "extra or rules_md_indexed"`
Expected: FAIL — `TypeError: unexpected keyword 'extra_files'`; 웹 테스트는 `indexed == 1`

- [ ] **Step 3: 구현**

`src/llmsearch/connectors/notes.py`:

```python
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ..models import Document, SyncResult
from ..rules import is_excluded


def _title_of(path: Path, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem


def _candidates(folders: list[Path], extra_files: Sequence[Path]) -> list[Path]:
    out: list[Path] = []
    for folder in folders:
        if folder.exists():
            out.extend(sorted(folder.rglob("*.md")))
    # rules.md 같은 단일 파일 — 존재하는 것만 (스펙 §9 "rules.md는 notes로 취급되어 인덱싱")
    out.extend(p for p in extra_files if p.exists())
    return out


def sync_notes(folders: list[Path], excludes: list[str], state: dict,
               extra_files: Sequence[Path] = ()) -> SyncResult:
    prev: dict[str, float] = dict(state.get("files", {}))
    seen: dict[str, float] = {}
    documents: list[Document] = []
    for path in _candidates(folders, extra_files):
        sid = str(path.resolve())
        if sid in seen:
            continue  # extra_files가 폴더 안 파일을 가리키면 중복 임베딩 방지
        if is_excluded(sid, None, path.parent.name, excludes):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        seen[sid] = mtime
        if prev.get(sid) == mtime:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        documents.append(
            Document(
                source_type="notes", source_id=sid, title=_title_of(path, text),
                text=text, url_or_path=sid,
                updated_at=datetime.fromtimestamp(mtime),
            )
        )
    deleted = [sid for sid in prev if sid not in seen]
    return SyncResult(documents=documents, deleted_ids=deleted, state={"files": seen})
```

`app.py` `run_sync`: `result = sync_notes(cfg.notes_folders, cfg.exclude, prev)` → `result = sync_notes(cfg.notes_folders, cfg.exclude, prev, extra_files=[cfg.rules_md_path])`.

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_notes.py tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/connectors/notes.py src/llmsearch/web/app.py tests/test_notes.py tests/test_web.py
git commit -m "feat: notes 커넥터 extra_files — rules.md를 notes로 인덱싱 (스펙 §9)"
```

---

### Task 6: 재요약 API(문서별/전체) + 출처 카드·설정 탭 버튼

**Files:**
- Modify: `src/llmsearch/connectors/local_docs.py` (센티널 공개 이름), `src/llmsearch/web/app.py`, `src/llmsearch/web/static/index.html`
- Test: `tests/test_web.py` (추가)

**Interfaces:**
- Consumes: Task 1 `_require_db`, Task 2 `local_origin_only`, Task 3 설정 탭 `#opsButtons`
- Produces: `local_docs.RETRY_SENTINEL = [0.0, 0]` (기존 `_RETRY_SENTINEL`은 별칭 유지); `GET /api/resummarize/count` → `{"count"}`; `POST /api/resummarize {"source_id"} | {"all": true}` → run_sync entry + `"reset"`; `state["resummarizing"]`; UI `resummarize(sid)`, `resummarizeAll()`, `#resumAllBtn`. Task 8 E2E가 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py` 끝에 추가:

```python
def make_app_with_docs(tmp_path: Path, monkeypatch) -> TestClient:
    """local_docs 감시 폴더 1개(pptx 스텁) — markitdown 대신 짧은 본문 스텁."""
    from llmsearch.connectors import local_docs

    monkeypatch.setattr(local_docs, "extract_text", lambda p: f"{p.stem} 본문. 프로젝트A 관련 내용 " * 10)
    watch = tmp_path / "watch"; watch.mkdir()
    (watch / "설계.pptx").write_bytes(b"x")
    (watch / "회의록.pptx").write_bytes(b"y")
    cfg = Config(data_dir=tmp_path / "data", watch_folders=[watch], projects=["프로젝트A"])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1")


def test_resummarize_one_overwrites_summary_without_duplicates(tmp_path: Path, monkeypatch):
    """스펙 M6 §4: 센티널 치환 → prior_map 유지 → 기존 요약 md 덮어쓰기(중복본 없음)."""
    from llmsearch import indexer

    client = make_app_with_docs(tmp_path, monkeypatch)
    assert client.post("/api/sync/local_docs").json()["indexed"] == 2
    state = client.app.state.llmsearch
    sid = str((tmp_path / "watch" / "설계.pptx").resolve())
    before_map = indexer.get_para_map(state["read_conn"], sid)
    summary_before = state["usage"].today_by_kind()["summary"]

    r = client.post("/api/resummarize", json={"source_id": sid})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["indexed"] == 1 and body["reset"] == 1
    assert state["usage"].today_by_kind()["summary"] == summary_before + 1
    assert indexer.get_para_map(state["read_conn"], sid) == before_map  # 같은 요약 md 경로에 덮어씀
    md_files = list((tmp_path / "data" / "summaries").rglob("설계*.md"))
    assert len(md_files) == 1, md_files  # 해시 접미사 중복본이 생기지 않는다
    files = indexer.get_sync_state(state["read_conn"], "local_docs")["files"]
    assert files[sid] != [0.0, 0]  # 재요약 후 실제 시그니처로 복귀


def test_resummarize_all_and_count(tmp_path: Path, monkeypatch):
    client = make_app_with_docs(tmp_path, monkeypatch)
    client.post("/api/sync/local_docs")
    assert client.get("/api/resummarize/count").json() == {"count": 2}
    state = client.app.state.llmsearch
    summary_before = state["usage"].today_by_kind()["summary"]
    body = client.post("/api/resummarize", json={"all": True}).json()
    assert body["reset"] == 2 and body["indexed"] == 2
    assert state["usage"].today_by_kind()["summary"] == summary_before + 2


def test_resummarize_unknown_and_foreign_origin(tmp_path: Path, monkeypatch):
    client = make_app_with_docs(tmp_path, monkeypatch)
    client.post("/api/sync/local_docs")
    assert client.post("/api/resummarize", json={"source_id": "/없음.pptx"}).status_code == 404
    assert client.post("/api/resummarize", json={}).status_code == 404
    assert client.post("/api/resummarize", json={"all": True},
                       headers={"Origin": "http://evil.example"}).status_code == 403


def test_resummarize_deleted_file_is_detected(tmp_path: Path, monkeypatch):
    """센티널 치환은 sid를 prev에 남기므로 그 사이 삭제된 파일의 deleted 판정이 살아 있다."""
    client = make_app_with_docs(tmp_path, monkeypatch)
    client.post("/api/sync/local_docs")
    sid = str((tmp_path / "watch" / "설계.pptx").resolve())
    (tmp_path / "watch" / "설계.pptx").unlink()
    body = client.post("/api/resummarize", json={"source_id": sid}).json()
    assert body["indexed"] == 0 and body["deleted"] == 1
    assert client.get("/api/resummarize/count").json() == {"count": 1}


def test_resummarize_respects_daily_limit_gate(tmp_path: Path, monkeypatch):
    client = make_app_with_docs(tmp_path, monkeypatch)
    client.post("/api/sync/local_docs")
    state = client.app.state.llmsearch
    state["usage"].daily_limit = 1  # 이미 초과 상태
    body = client.post("/api/resummarize", json={"all": True}).json()
    assert body["ok"] is False and "일일 API 호출 상한" in body["error"] and body["reset"] == 2
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v -k resummarize`
Expected: FAIL — `/api/resummarize` 404

- [ ] **Step 3: 구현**

`src/llmsearch/connectors/local_docs.py` — 센티널을 공개 이름으로:

```python
RETRY_SENTINEL = [0.0, 0]
_RETRY_SENTINEL = RETRY_SENTINEL  # 기존 참조 호환
```

`src/llmsearch/web/app.py` — import에 `from ..connectors.local_docs import RETRY_SENTINEL, sync_local_docs`. 엔드포인트(`/api/rules` 뒤):

```python
    @app.get("/api/resummarize/count")
    def resummarize_count():
        _require_db()
        files = indexer.get_sync_state(state["read_conn"], "local_docs").get("files", {})
        return {"count": len(files)}

    @app.post("/api/resummarize", dependencies=[Depends(local_origin_only)])
    def resummarize(payload: dict):
        """문서별/전체 재요약 (스펙 §9, M6 §4).

        상태 항목을 제거하지 않고 RETRY_SENTINEL로 치환한다 — sid가 prev에 남아야 run_sync의
        prior_map이 유지되어 기존 요약 md를 덮어쓰고(제거하면 해시 접미사 중복본 생성),
        실제 시그니처와 불일치해 재요약이 강제되며, 그 사이 삭제된 파일의 deleted 판정도 산다.
        """
        _require_db()
        if state.get("resummarizing"):
            raise HTTPException(409, "재요약이 이미 진행 중입니다")
        state["resummarizing"] = True
        try:
            with state["sync_lock"]:
                st = indexer.get_sync_state(state["conn"], "local_docs")
                files = dict(st.get("files", {}))
                if payload.get("all") is True:
                    targets = list(files)
                else:
                    sid = str(payload.get("source_id", ""))
                    if sid not in files:
                        raise HTTPException(404, "local_docs 인덱스에 없는 문서입니다")
                    targets = [sid]
                for sid in targets:
                    files[sid] = list(RETRY_SENTINEL)
                indexer.set_sync_state(state["conn"], "local_docs", {**st, "files": files})
            entry = run_sync(state, "local_docs")  # 상한 게이트·오류 격리·로그 그대로 적용
            return {**entry, "reset": len(targets)}
        finally:
            state["resummarizing"] = False
```

`index.html` — 출처 카드 렌더링(`ev === 'sources'` 루프)에서 열기 버튼 뒤에 재요약 버튼:

```js
          const resum = h.source_type === 'local_docs'
            ? ` <button onclick="resummarize(this.dataset.s)" data-s="${esc(h.source_id)}">재요약</button>` : '';
          answerDiv.insertAdjacentHTML('beforeend',
            `<div class="src">📄 ${esc(h.title)}${lock} <small>(${esc(h.source_type)} · ${esc(h.updated_at)})</small><br>` +
            `<code>${esc(h.url_or_path)}</code> ` +
            `<button onclick="openItem(this.dataset.p)" data-p="${esc(h.url_or_path)}">열기</button>${resum}</div>`);
```

설정 탭 `#opsButtons`에 `<button id="resumAllBtn" onclick="resummarizeAll()">전체 재요약</button>` (HTML에 직접). JS:

```js
function resumMessage(r, d) {
  if (!r.ok) return d.detail || '실패';
  if (!d.ok) return d.error;
  return `재요약 완료: ${d.indexed}건` + (d.indexed === 0 ? ' (파일이 이미 없거나 상한/오류 — 로그 탭 확인)' : '');
}
async function resummarize(sid) {
  if (!confirm('이 문서를 다시 요약할까요? 요약 API를 호출합니다.')) return;
  const r = await fetch('/api/resummarize', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({source_id: sid})});
  alert(resumMessage(r, await r.json()));
}
async function resummarizeAll() {
  const c = (await (await fetch('/api/resummarize/count')).json()).count;
  if (!confirm(`${c}건을 다시 요약합니다. 요약 API를 최소 ${c}회(비전 문서는 +1) 호출합니다.`)) return;
  const btn = document.getElementById('resumAllBtn'); btn.disabled = true;
  try {
    const r = await fetch('/api/resummarize', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({all: true})});
    alert(resumMessage(r, await r.json()));
  } finally { btn.disabled = false; }
}
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_web.py tests/test_local_docs.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/connectors/local_docs.py src/llmsearch/web/app.py src/llmsearch/web/static/index.html tests/test_web.py
git commit -m "feat: 재요약 API — 센티널 치환으로 요약 md 덮어쓰기, 출처 카드·설정 탭 버튼 (스펙 §9, M6 §4)"
```

---

### Task 7: 사용량 GUI 표시 — `recent_days`·`/api/usage`·소스 탭 한 줄

**Files:**
- Modify: `src/llmsearch/usage.py`, `src/llmsearch/web/app.py`, `src/llmsearch/web/static/index.html`
- Test: `tests/test_usage.py`, `tests/test_web.py` (추가)

**Interfaces:**
- Produces: `UsageTracker.recent_days(n: int) -> list[tuple[str, int]]`; `GET /api/usage` → `{"today", "total", "limit", "indexing_allowed", "days"}`; UI `#usageLine`, `loadUsage()`. Task 8 E2E가 `#usageLine`을 읽는다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_usage.py` 끝에 추가:

```python
def test_recent_days_window_and_order(tmp_path: Path):
    from datetime import timedelta

    today = date.today()
    data = {
        (today - timedelta(days=10)).isoformat(): {"embed": 99},
        (today - timedelta(days=6)).isoformat(): {"embed": 2, "answer": 1},
        today.isoformat(): {"summary": 4},
    }
    path = tmp_path / "usage.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    t = UsageTracker(path)
    assert t.recent_days(7) == [((today - timedelta(days=6)).isoformat(), 3), (today.isoformat(), 4)]
    assert t.recent_days(1) == [(today.isoformat(), 4)]
    assert UsageTracker(tmp_path / "none.json").recent_days(7) == []
```

`tests/test_web.py` 끝에 추가:

```python
def test_usage_endpoint_and_line(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    client.post("/api/chat", json={"question": "q", "history": []})
    u = client.get("/api/usage").json()
    assert u["today"]["embed"] >= 2 and u["today"]["answer"] == 1
    assert u["total"] == sum(u["today"].values())
    assert u["limit"] == 0 and u["indexing_allowed"] is True
    assert u["days"][-1]["date"] == client.app.state.llmsearch["usage"]._today()
    assert u["days"][-1]["total"] == u["total"]
    assert 'id="usageLine"' in client.get("/").text
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_usage.py tests/test_web.py -v -k "recent_days or usage_endpoint"`
Expected: FAIL — `AttributeError: recent_days`, `/api/usage` 404

- [ ] **Step 3: 구현**

`src/llmsearch/usage.py` — import에 `from datetime import date, timedelta`; `UsageTracker`에 메서드 추가(`today_by_kind` 아래):

```python
    def recent_days(self, n: int) -> list[tuple[str, int]]:
        """최근 n일(오늘 포함) (날짜, 합계) 오름차순 — 기록 없는 날은 제외. GUI 표시용 (스펙 §10)."""
        cutoff = (date.today() - timedelta(days=n - 1)).isoformat()
        with self._lock:
            return [(day, sum(kinds.values()))
                    for day, kinds in sorted(self._data.items()) if day >= cutoff]
```

`app.py` 엔드포인트(`/api/log` 뒤):

```python
    @app.get("/api/usage")
    def usage_status():
        t: UsageTracker = state["usage"]
        return {"today": t.today_by_kind(), "total": t.today_total(), "limit": t.daily_limit,
                "indexing_allowed": t.indexing_allowed(),
                "days": [{"date": d, "total": n} for d, n in t.recent_days(7)]}
```

`index.html` — 소스 탭 `<table id="srcTable">` 바로 위에 `<div id="usageLine"></div>`. `loadSources()` 첫 줄에 `loadUsage();` 추가. JS:

```js
async function loadUsage() {
  const u = await (await fetch('/api/usage')).json();
  const kinds = ['embed', 'summary', 'vision', 'answer'].map(k => `${k} ${u.today[k] || 0}`).join(' · ');
  let line = `오늘 API 호출 ${u.total}건 (${kinds}) · ` +
    (u.limit > 0 ? `일일 상한 ${u.total} / ${u.limit}건` : '일일 상한 없음');
  if (!u.indexing_allowed) line += ' ⚠️ 상한 도달 — 요약·인덱싱 일시정지 (검색·답변은 계속)';
  document.getElementById('usageLine').textContent = line;
}
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_usage.py tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/usage.py src/llmsearch/web/app.py src/llmsearch/web/static/index.html tests/test_usage.py tests/test_web.py
git commit -m "feat: 사용량 GUI 표시 — recent_days·GET /api/usage·소스 탭 한 줄 (스펙 §10)"
```

---

### Task 8: E2E 확장 — 설정 탭·사용량 표시·재요약 (9단계와 10단계 사이)

**Files:**
- Modify: `tools/e2e/verify.py` (기존 45건 문면 무변경; 9단계 블록 끝과 `# 10.` 주석 사이에 삽입)
- Modify: `docs/HANDOFF.md` (E2E 건수)

**Interfaces:**
- Consumes: Task 3 `#rulesText/#saveRulesBtn/#rulesStatus`, Task 6 `.src button "재요약"`·`#resumAllBtn`, Task 7 `#usageLine`; verify.py 2.5단계의 `usage_today()`

- [ ] **Step 1: 시나리오 삽입**

`# 10. 일일 상한 게이트` 주석 바로 앞에:

```python
    # 9.5 M6a — 설정 탭 rules.md 편집·재로드 (스펙 §9)
    page.click("nav >> text=설정")
    page.wait_for_selector("#rulesText")
    page.wait_for_timeout(300)
    check("설정 탭 템플릿 로드", page.input_value("#rulesText").startswith("# 규칙 (rules.md)"))
    page.fill("#rulesText", "# 규칙 (rules.md)\n\n## 용어집\nPJA = 프로젝트A\n\n## 분류 규칙\n\n"
                            "## 요약 규칙\n수치는 표로\n\n## 답변 규칙\n두괄식\n")
    page.click("#saveRulesBtn")
    page.wait_for_timeout(300)
    status = page.locator("#rulesStatus").inner_text()
    check("규칙 저장 상태", "저장됨" in status and "요약 규칙" in status, status)
    page.click("nav >> text=채팅")
    page.click("nav >> text=설정")
    page.wait_for_timeout(300)
    check("규칙 재로드 일치", "PJA = 프로젝트A" in page.input_value("#rulesText"))

    # 9.6 M6a — 사용량 한 줄 표시 (스펙 §10 GUI)
    page.click("nav >> text=소스")
    page.wait_for_selector("#usageLine")
    page.wait_for_timeout(300)
    usage_line = page.locator("#usageLine").inner_text()
    check("사용량 표시", "오늘 API 호출" in usage_line and "embed" in usage_line
          and f"/ {DAILY_LIMIT}건" in usage_line, usage_line)

    # 9.7 M6a — 출처 카드 재요약: 데모 pptx는 비전 경로라 summary +1, vision +1
    before = usage_today()
    page.click("nav >> text=채팅")
    page.fill("#question", "프로젝트A 아키텍처 표지")
    page.click("text=검색")
    page.wait_for_selector(".src button:has-text('재요약')", timeout=10000)
    dialogs.clear()
    page.locator(".src button", has_text="재요약").first.click()  # confirm은 기존 dialog 핸들러가 accept
    page.wait_for_timeout(1500)
    after = usage_today()
    check("문서 재요약: summary +1", after.get("summary", 0) == before.get("summary", 0) + 1,
          f"{before.get('summary')}→{after.get('summary')}")
    check("문서 재요약: vision +1", after.get("vision", 0) == before.get("vision", 0) + 1,
          f"{before.get('vision')}→{after.get('vision')}")
    check("문서 재요약: 완료 alert", any("재요약 완료: 1건" in m for m in dialogs),
          " / ".join(m[:40] for m in dialogs))
    md_files = list((DATA / "data" / "summaries").rglob("프로젝트A_아키텍처*.md"))
    check("재요약 후 요약 md 중복 없음", len(md_files) == 1, str(md_files))

    # 9.8 M6a — 설정 탭 전체 재요약: confirm 문구에 건수, 실행 후 summary +1
    before = usage_today()
    dialogs.clear()
    page.click("nav >> text=설정")
    page.click("#resumAllBtn")
    page.wait_for_timeout(1500)
    check("전체 재요약 confirm 건수", any("1건을 다시 요약" in m for m in dialogs),
          " / ".join(m[:40] for m in dialogs))
    check("전체 재요약: summary +1", usage_today().get("summary", 0) == before.get("summary", 0) + 1)
```

- [ ] **Step 2: 실행 검증**

```bash
./.venv/bin/python tools/e2e/demo_server.py &   # 기동 대기
./.venv/bin/python tools/e2e/verify.py
```

Expected: `총 56건 전부 PASS` (45 + 11). 예산: 기존 ≈12건 + 채팅 2 + 재요약 3 + 전체 재요약 3 ≈ 20 < 50, 10단계 루프 여유 충분. 서버 종료.

- [ ] **Step 3: HANDOFF 갱신**

`docs/HANDOFF.md` §1 표 M5 행 아래에 `| M6a 운영 완성(설정·재요약·사용량) | ✅ 머지 | rules.md 설정 탭·요약 규칙 주입·notes 인덱싱, 재요약(센티널), 사용량 표시, 로컬 오리진 검사 |` 추가, "master 테스트 기준"과 E2E 건수를 실제 값으로 갱신, §5 문서 지도에 M6 스펙·로드맵·계획 경로 추가, §3을 "다음 작업: M6b (`docs/superpowers/plans/2026-08-29-llmsearch-m6b.md` — 작성 예정)"로.

- [ ] **Step 4: Commit**

```bash
git add tools/e2e/verify.py docs/HANDOFF.md
git commit -m "test: E2E 확장 — M6a 설정 탭·사용량 표시·재요약 시나리오 (전 항목 PASS)"
```

---

## M6a 수동 체크리스트 (실환경 — 머지 후 사용자 확인)

1. 설정 탭에서 `## 답변 규칙`을 바꾸고 저장 → 재시작 없이 다음 채팅 답변의 문체가 바뀌는지
2. `## 요약 규칙`에 "수치는 표로" 추가 → 출처 카드 "재요약" → 요약 md에 표가 생기는지 (실 Gemini)
3. 소스 탭 상단 사용량 줄이 동기화·채팅마다 갱신되는지, `limits.daily_api_calls` 설정 시 "n / m건"으로 바뀌는지
