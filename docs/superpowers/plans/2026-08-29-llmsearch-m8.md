# llmsearch M8 — 채팅 UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스펙 M8 — 대화 세션 저장/목록/복원(`chats.db`), `/api/chat` 세션 통합(서버 이력·선저장·`finally` 저장), 내보내기(결정적 md + notes 옵션), 출처 미리보기 dialog, E2E.

**Architecture:** `chats.py` `ChatStore`가 `data_dir/chats.db`(인덱스와 분리, rebuild 무관)를 자체 락·커넥션으로 관리한다. `/api/chat`은 `session_id`가 있으면 서버가 이력을 구성하고 user를 스트림 전에, assistant를 `event_stream`의 `finally`에서 저장한다(중단 시 부분 보존, 빈 답변은 자리표시). 내보내기는 `chat-<id>-<slug>.md`로 결정적(재내보내기 덮어쓰기)이며 `[대화기록]` 접두어·고지로 자기참조 오염을 막는다. 미리보기는 SSE로 이미 받은 `excerpt`를 클라이언트가 표시한다(엔드포인트 없음).

**Tech Stack:** Python 3.12, FastAPI, SQLite, Playwright E2E — 신규 의존성 없음

**Spec:** `docs/superpowers/specs/2026-08-29-llmsearch-m8-design.md`

**계획 시점 추가(스펙 API 보강):** `ChatStore.get_title(session_id) -> str`(없으면 `KeyError`) — `/api/chat`의 "제목이 '새 대화'면 첫 질문으로" 판정과 export 파일명에 필요. 스펙 §2 API 목록에 준한다.

## Global Constraints

- `chats.db`는 `index.db`와 별개 파일 — `rebuild.py`는 절대 건드리지 않는다(테스트로 보존 확인)
- `ChatStore` 커넥션: `PRAGMA journal_mode=WAL` + `PRAGMA foreign_keys=ON`; 읽기·쓰기 모두 자체 락; `AUTOINCREMENT`; 2계층 삭제
- `/api/chat` 순서: 필터 검증 → `session_id` 검증(bool 제외 int, 404) → `history` 확정 → `record("answer")` → user 선저장(+제목) → 스트림 → assistant는 `finally`(빈 답변은 `"(답변 없음 — 응답 전 중단)"`), `saved`는 정상 경로만, `finally`에서 `yield` 금지
- `history(limit=20, max_chars=40_000)`: 선두 assistant 제거 → 초과 시 가장 오래된 2건씩 제거 → 선두 규칙 재적용
- export: `chat-<id>-<slug>.md`, slug = `_sanitize_segment` → `[^0-9A-Za-z가-힣\-_]`→`_` → 40자 → 빈 값 `chat`; `relative_to` 2계층; tmp+`os.replace`; 첫 줄 `# [대화기록] <제목>` + 고지
- UI 동적 값은 `textContent`/`esc()`만; 상태 변경 API는 `local_origin_only`; 복원·[새 대화]·삭제 시 클라이언트 `history` 배열 비움
- 웹 테스트 `TestClient(base_url="http://127.0.0.1")`; embed 카운트 단언 없음(질의 캐시 무관) — 단 질문 문자열은 스위트 내 유일하게
- Python 4칸 들여쓰기, 표준 라이브러리만, 기존 344 테스트 무변경 통과, 태스크마다 전체 green; E2E 기존 73건 무변경, 신규는 9.11 뒤 `# 10.` 앞
- 커밋 메시지 한국어, `feat:`/`test:` 접두사

---

### Task 1: `chats.py` — `ChatStore`

**Files:**
- Create: `src/llmsearch/chats.py`
- Test: `tests/test_chats.py` (신규)

**Interfaces:**
- Produces: `chats.SCHEMA_VERSION = "1"`, `chats.DEFAULT_TITLE = "새 대화"`, `chats.TITLE_MAX = 60`, `chats.normalize_title(text) -> str`, `chats.filters_label(filters) -> str`, `ChatStore(path)` with `create_session(title=DEFAULT_TITLE) -> int`, `get_title(id) -> str`, `set_title(id, title)`, `list_sessions(limit=50)`, `get_session(id)`, `append(id, role, content, sources=None, filters=None) -> int`, `history(id, limit=20, max_chars=40_000)`, `delete_session(id) -> bool`, `export_markdown(id) -> str`, `close()`. `KeyError` = 세션 없음, `ValueError` = role 비정상, `RuntimeError` = 스키마 불일치. Task 2·3이 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_chats.py` 신규:

```python
import sqlite3
import threading
from pathlib import Path

import pytest

from llmsearch import chats
from llmsearch.chats import ChatStore


def test_create_list_get_and_title_normalization(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    a = s.create_session()
    b = s.create_session("  프로젝트A   회의록   " + "x" * 100)
    assert s.get_title(a) == "새 대화"
    assert s.get_title(b) == ("프로젝트A 회의록 " + "x" * 100)[:60]
    lst = s.list_sessions()
    assert [x["id"] for x in lst] == [b, a] and lst[0]["message_count"] == 0
    got = s.get_session(a)
    assert got["id"] == a and got["title"] == "새 대화" and got["messages"] == []
    with pytest.raises(KeyError):
        s.get_session(999)
    with pytest.raises(KeyError):
        s.get_title(999)


def test_append_updates_order_and_roundtrips_sources_filters(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    a = s.create_session("A")
    b = s.create_session("B")
    assert [x["id"] for x in s.list_sessions()] == [b, a]
    s.append(a, "user", "질문", filters={"source_filter": ["notes"], "date_from": None, "date_to": None, "sender": None})
    s.append(a, "assistant", "답변", sources=[{"title": "t", "source_type": "notes", "url_or_path": "/n/a.md", "excerpt": "본문"}])
    assert [x["id"] for x in s.list_sessions()] == [a, b]  # append가 updated_at 갱신
    msgs = s.get_session(a)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["filters"]["source_filter"] == ["notes"] and msgs[0]["sources"] == []
    assert msgs[1]["sources"][0]["excerpt"] == "본문" and msgs[1]["filters"] is None
    assert s.list_sessions()[0]["message_count"] == 2
    with pytest.raises(ValueError):
        s.append(a, "system", "x")
    with pytest.raises(KeyError):
        s.append(999, "user", "x")
    s.set_title(a, "  새  제목 ")
    assert s.get_title(a) == "새 제목"
    with pytest.raises(KeyError):
        s.set_title(999, "x")


def test_history_limit_leading_user_and_char_cap(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    sid = s.create_session()
    for i in range(12):
        s.append(sid, "user", f"q{i}")
        s.append(sid, "assistant", f"a{i}", sources=[{"excerpt": "x"}])
    h = s.history(sid, limit=5)  # 마지막 5건: a9,q10,a10,q11,a11 → 선두 assistant 제거
    assert [m["content"] for m in h] == ["q10", "a10", "q11", "a11"]
    assert all(set(m) == {"role", "content"} for m in h)  # sources 제외
    h = s.history(sid, limit=6, max_chars=7)  # q9..a11(6건) → 2건씩 제거: 6→4→2 (q11,a11)
    assert [m["content"] for m in h] == ["q11", "a11"]
    orphan = s.create_session()
    s.append(orphan, "user", "u1"); s.append(orphan, "user", "u2"); s.append(orphan, "assistant", "a2")
    assert [m["role"] for m in s.history(orphan)] == ["user", "user", "assistant"]  # 연속 user 허용
    assert s.history(orphan, limit=1) == []  # 마지막 1건이 assistant → 제거 후 빈 목록
    with pytest.raises(KeyError):
        s.history(999)


def test_delete_two_layer_and_no_id_reuse(tmp_path: Path):
    path = tmp_path / "chats.db"
    s = ChatStore(path)
    a = s.create_session("A")
    s.append(a, "user", "q"); s.append(a, "assistant", "a")
    assert s.delete_session(a) is True and s.delete_session(a) is False
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0  # 메시지도 삭제됨
    raw.close()
    b = s.create_session("B")
    assert b != a and s.get_session(b)["messages"] == []  # id 재사용 없음 → 삭제된 대화 부활 불가


def test_export_markdown_format(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    sid = s.create_session("<제목>")
    s.append(sid, "user", "첫 질문", filters={"source_filter": ["notes"], "date_from": "2026-08-01", "date_to": None, "sender": None})
    s.append(sid, "assistant", "첫 답변", sources=[{"source_type": "notes", "title": "킥오프", "url_or_path": "/n/k.md", "excerpt": "..."}])
    s.append(sid, "user", "둘째 질문")
    s.append(sid, "assistant", "둘째 답변")
    md = s.export_markdown(sid)
    lines = md.splitlines()
    assert lines[0] == "# [대화기록] <제목>" and lines[1].startswith("> 이 문서는 llmsearch가 생성한 답변 기록입니다")
    assert "## Q1. 첫 질문" in md and "(필터: 소스=notes · 기간=2026-08-01~)" in md
    assert "출처:\n- [notes] 킥오프 — /n/k.md" in md
    assert "## Q2. 둘째 질문" in md and md.index("## Q1.") < md.index("## Q2.")
    assert "(필터:" not in md.split("## Q2.")[1]  # 필터 없는 턴엔 필터 줄 없음
    with pytest.raises(KeyError):
        s.export_markdown(999)


def test_schema_version_created_and_mismatch_rejected(tmp_path: Path):
    path = tmp_path / "chats.db"
    ChatStore(path).close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == chats.SCHEMA_VERSION
    raw.execute("UPDATE meta SET value='0' WHERE key='schema_version'"); raw.commit(); raw.close()
    with pytest.raises(RuntimeError):
        ChatStore(path)


def test_concurrent_append_is_serialized(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    sid = s.create_session()

    def worker(n):
        for i in range(20):
            s.append(sid, "user", f"{n}-{i}")

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert s.list_sessions()[0]["message_count"] == 100
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_chats.py -v`
Expected: FAIL — `ModuleNotFoundError: llmsearch.chats`

- [ ] **Step 3: 구현**

`src/llmsearch/chats.py` 신규:

```python
"""대화 세션 저장소 — data_dir/chats.db (스펙 M8 §2).

인덱스(index.db)와 분리한다: 인덱스는 소모품이라 rebuild가 지우지만 대화는 사용자 산출물이다.
단일 커넥션을 웹 스레드풀이 공유하므로 읽기·쓰기 모두 자체 락으로 직렬화한다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "1"
DEFAULT_TITLE = "새 대화"
TITLE_MAX = 60
ROLES = ("user", "assistant")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]', filters_json TEXT NOT NULL DEFAULT 'null',
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


def normalize_title(text: str) -> str:
    """공백 정규화 후 60자 절단, 빈 값이면 기본 제목."""
    return " ".join((text or "").split())[:TITLE_MAX] or DEFAULT_TITLE


def filters_label(filters: dict | None) -> str:
    """저장된 필터를 한 줄로 — UI filtersLabel()과 같은 형식 (export용)."""
    if not filters:
        return ""
    parts = []
    if filters.get("source_filter"):
        parts.append("소스=" + ",".join(filters["source_filter"]))
    if filters.get("date_from") or filters.get("date_to"):
        parts.append(f"기간={filters.get('date_from') or ''}~{filters.get('date_to') or ''}")
    if filters.get("sender"):
        parts.append("발신자=" + filters["sender"])
    return " · ".join(parts)


def _now() -> str:
    return datetime.now().isoformat()


class ChatStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")  # 커넥션 단위 설정 — cascade가 실제로 동작하게
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            self._conn.commit()
        elif row[0] != SCHEMA_VERSION:
            self._conn.close()
            raise RuntimeError(f"chats.db schema v{row[0]} != v{SCHEMA_VERSION}")

    # --- 내부 (락 보유 상태에서 호출) ---
    def _require(self, session_id: int) -> tuple:
        row = self._conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row

    def _messages(self, session_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, role, content, sources_json, filters_json, created_at FROM messages "
            "WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2], "sources": json.loads(r[3]),
                 "filters": json.loads(r[4]), "created_at": r[5]} for r in rows]

    # --- 공개 API ---
    def create_session(self, title: str = DEFAULT_TITLE) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute("INSERT INTO sessions(title, created_at, updated_at) VALUES (?,?,?)",
                                     (normalize_title(title), now, now))
            self._conn.commit()
            return cur.lastrowid

    def get_title(self, session_id: int) -> str:
        with self._lock:
            return self._require(session_id)[1]

    def set_title(self, session_id: int, title: str) -> None:
        with self._lock:
            self._require(session_id)
            self._conn.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                               (normalize_title(title), _now(), session_id))
            self._conn.commit()

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.title, s.created_at, s.updated_at, "
                "(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) "
                "FROM sessions s ORDER BY s.updated_at DESC, s.id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3], "message_count": r[4]}
                for r in rows]

    def get_session(self, session_id: int) -> dict:
        with self._lock:
            sid, title, created, updated = self._require(session_id)
            return {"id": sid, "title": title, "created_at": created, "updated_at": updated,
                    "messages": self._messages(session_id)}

    def append(self, session_id: int, role: str, content: str,
               sources: list[dict] | None = None, filters: dict | None = None) -> int:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        now = _now()
        with self._lock:
            self._require(session_id)
            cur = self._conn.execute(
                "INSERT INTO messages(session_id, role, content, sources_json, filters_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, role, content, json.dumps(sources or [], ensure_ascii=False),
                 json.dumps(filters, ensure_ascii=False), now))
            self._conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            self._conn.commit()
            return cur.lastrowid

    def history(self, session_id: int, limit: int = 20, max_chars: int = 40_000) -> list[dict]:
        """Claude 컨텍스트용 이력 — sources 제외. 첫 메시지는 반드시 user(Messages API 규칙),
        누적 길이가 max_chars를 넘으면 가장 오래된 메시지부터 role 무관하게 2건씩 제거."""
        with self._lock:
            self._require(session_id)
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit)).fetchall()
        msgs = [{"role": r, "content": c} for r, c in reversed(rows)]
        if msgs and msgs[0]["role"] == "assistant":
            msgs.pop(0)
        while len(msgs) > 2 and sum(len(m["content"]) for m in msgs) > max_chars:
            del msgs[:2]
        if msgs and msgs[0]["role"] == "assistant":
            msgs.pop(0)
        return msgs

    def delete_session(self, session_id: int) -> bool:
        with self._lock:
            # cascade에 의존하지 않는 2계층 삭제 — FK PRAGMA가 꺼진 커넥션으로 열려도 고아 메시지 없음
            self._conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            cur = self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def export_markdown(self, session_id: int) -> str:
        with self._lock:
            _, title, created, _ = self._require(session_id)
            messages = self._messages(session_id)
        lines = [
            f"# [대화기록] {title}",
            "> 이 문서는 llmsearch가 생성한 답변 기록입니다 — 1차 출처가 아닙니다. 원 출처는 각 답변 하단 목록을 확인하세요.",
            f"- 생성: {created} · 내보내기: {_now()}",
            "",
        ]
        n = 0
        for m in messages:
            if m["role"] == "user":
                n += 1
                lines.append(f"## Q{n}. {m['content']}")
                label = filters_label(m["filters"])
                if label:
                    lines.append(f"(필터: {label})")
            else:
                lines += ["", m["content"], ""]
                if m["sources"]:
                    lines.append("출처:")
                    lines += [f"- [{s.get('source_type', '')}] {s.get('title', '')} — {s.get('url_or_path', '')}"
                              for s in m["sources"]]
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def close(self) -> None:
        with self._lock:
            self._conn.close()
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_chats.py -v` → PASS, `./.venv/bin/pytest -q` → 344 + 7 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/chats.py tests/test_chats.py
git commit -m "feat: ChatStore — chats.db 세션·메시지 저장, history 규칙, 2계층 삭제, export md (스펙 M8 §2)"
```

---

### Task 2: `/api/chats` CRUD + `/api/chat` 세션 통합

**Files:**
- Modify: `src/llmsearch/web/app.py`, `src/llmsearch/llm.py`(`FakeAnswerer.last_history`)
- Test: `tests/test_web.py` (추가)

**Interfaces:**
- Consumes: Task 1 `ChatStore`
- Produces: `state["chat_store"]`(None이면 폴백)·`state["chat_store_error"]`; `_require_chat_store()`(503); `GET/POST /api/chats`, `GET/DELETE /api/chats/{id}`; `/api/chat` 페이로드 `session_id`; SSE `event: saved`; 모듈 함수 `_save_assistant(store, session_id, parts, hits) -> bool`; `FakeAnswerer.last_history`. Task 3·4가 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py` 끝에 추가:

```python
def _sse_events(body: str) -> list[str]:
    return [l[len("event: "):] for l in body.splitlines() if l.startswith("event: ")]


def test_chats_crud(tmp_path: Path):
    client = make_app(tmp_path)
    assert client.get("/api/chats").json() == []
    r = client.post("/api/chats", json={})
    assert r.status_code == 200 and r.json()["title"] == "새 대화"
    a = r.json()["id"]
    b = client.post("/api/chats", json={"title": "  둘째   세션 "}).json()["id"]
    assert [s["id"] for s in client.get("/api/chats").json()] == [b, a]
    assert client.get("/api/chats").json()[0]["title"] == "둘째 세션"
    assert client.post("/api/chats", json={"title": 5}).status_code == 400
    assert client.post("/api/chats", json={"title": "x" * 201}).status_code == 400
    assert client.get(f"/api/chats/{a}").json()["messages"] == []
    assert client.get("/api/chats/999").status_code == 404
    assert client.get("/api/chats/abc").status_code == 422
    assert client.delete(f"/api/chats/{a}").json() == {"ok": True}
    assert client.delete(f"/api/chats/{a}").status_code == 404
    evil = {"Origin": "http://evil.example"}
    assert client.post("/api/chats", json={}, headers=evil).status_code == 403
    assert client.delete(f"/api/chats/{b}", headers=evil).status_code == 403


def test_chats_503_when_store_unavailable_but_chat_works(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    state = client.app.state.llmsearch
    state["chat_store"] = None
    state["chat_store_error"] = "OperationalError"
    r = client.get("/api/chats")
    assert r.status_code == 503 and "OperationalError" in r.json()["detail"]
    assert client.post("/api/chats", json={}).status_code == 503
    assert client.post("/api/chat", json={"question": "저장소 없음 질의", "history": [], "session_id": 1}).status_code == 503
    r = client.post("/api/chat", json={"question": "저장소 없음 무세션 질의", "history": []})
    assert r.status_code == 200 and "event: done" in r.text and "event: saved" not in r.text


def test_chat_with_session_saves_and_uses_server_history(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    state = client.app.state.llmsearch
    sid = client.post("/api/chats", json={}).json()["id"]
    r = client.post("/api/chat", json={"question": "세션 첫 질문 킥오프", "session_id": sid,
                                       "history": [{"role": "user", "content": "무시되어야 함"}],
                                       "filters": {"source_filter": ["notes"]}})
    assert r.status_code == 200
    assert _sse_events(r.text)[-2:] == ["saved", "done"]
    assert state["answerer"].last_history == []  # 첫 질문: 서버 이력 비어 있음, 페이로드 history 무시
    s = client.get(f"/api/chats/{sid}").json()
    assert s["title"] == "세션 첫 질문 킥오프"  # "새 대화" → 첫 질문
    assert [m["role"] for m in s["messages"]] == ["user", "assistant"]
    assert s["messages"][0]["filters"]["source_filter"] == ["notes"]
    assert s["messages"][1]["sources"] and s["messages"][1]["sources"][0]["source_type"] == "notes"
    assert "excerpt" in s["messages"][1]["sources"][0] and s["messages"][1]["content"].startswith("[1]")
    client.post("/api/chat", json={"question": "세션 둘째 질문", "session_id": sid})
    assert [m["content"] for m in state["answerer"].last_history] == ["세션 첫 질문 킥오프", s["messages"][1]["content"]]
    assert client.get(f"/api/chats/{sid}").json()["title"] == "세션 첫 질문 킥오프"  # 제목은 첫 질문 유지


def test_chat_without_session_is_stateless(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    hist = [{"role": "user", "content": "이전"}, {"role": "assistant", "content": "답"}]
    r = client.post("/api/chat", json={"question": "무세션 질의", "history": hist})
    assert r.status_code == 200 and "event: saved" not in r.text
    assert client.app.state.llmsearch["answerer"].last_history == hist
    assert client.get("/api/chats").json() == []


def test_chat_bad_session_id_404_and_no_answer_count(tmp_path: Path):
    client = make_app(tmp_path)
    before = client.app.state.llmsearch["usage"].today_by_kind().get("answer", 0)
    for bad in (True, 999, "1", 1.5):
        assert client.post("/api/chat", json={"question": "q", "session_id": bad}).status_code == 404, bad
    assert client.app.state.llmsearch["usage"].today_by_kind().get("answer", 0) == before


class _ExplodingAnswerer(FakeAnswerer):
    def __init__(self, after: int):
        super().__init__()
        self.after = after  # 이 개수의 text 이벤트 뒤에 폭발 (0이면 첫 토큰 전)

    def answer_stream(self, question, history, search_fn, filters_note: str = ""):
        self.last_history = history
        for i in range(self.after):
            yield {"type": "text", "text": f"부분{i} "}
        raise RuntimeError("stream broke")


def _app_with_answerer(tmp_path: Path, answerer) -> TestClient:
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "kick.md").write_text("# 킥오프\n내용", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(), answerer=answerer, enable_scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)


def test_chat_partial_answer_saved_in_finally(tmp_path: Path):
    client = _app_with_answerer(tmp_path, _ExplodingAnswerer(after=2))
    sid = client.post("/api/chats", json={}).json()["id"]
    try:
        with client.stream("POST", "/api/chat", json={"question": "중단 질의", "session_id": sid}) as r:
            body = "".join(r.iter_text())
    except Exception:
        body = ""
    assert "event: saved" not in body
    msgs = client.get(f"/api/chats/{sid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "부분0 부분1 "  # 부분 답변 보존


def test_chat_empty_answer_saves_placeholder(tmp_path: Path):
    client = _app_with_answerer(tmp_path, _ExplodingAnswerer(after=0))
    sid = client.post("/api/chats", json={}).json()["id"]
    try:
        with client.stream("POST", "/api/chat", json={"question": "즉시 중단 질의", "session_id": sid}) as r:
            "".join(r.iter_text())
    except Exception:
        pass
    msgs = client.get(f"/api/chats/{sid}").json()["messages"]
    assert msgs[1]["content"] == "(답변 없음 — 응답 전 중단)"  # 빈 text 블록 방지
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v -k "chats or session or stateless or partial or placeholder"`
Expected: FAIL — `/api/chats` 404, `AttributeError: last_history`

- [ ] **Step 3: 구현**

`src/llmsearch/llm.py` `FakeAnswerer.__init__`에 `self.last_history: list | None = None`; `answer_stream` 첫 줄에 `self.last_history = list(history)`.

`src/llmsearch/web/app.py` — import `from ..chats import DEFAULT_TITLE, ChatStore, normalize_title`. 모듈 함수:

```python
EMPTY_ANSWER_PLACEHOLDER = "(답변 없음 — 응답 전 중단)"  # Messages API는 빈 text 블록을 거부한다


def _save_assistant(store: ChatStore, session_id: int, parts: list[str], hits: list) -> bool:
    """assistant 턴 저장 — 정상 종료·중단 공통. 실패는 로그(클래스명)만."""
    text = "".join(parts) or EMPTY_ANSWER_PLACEHOLDER
    try:
        store.append(session_id, "assistant", text, sources=[asdict(h) for h in hits])
        return True
    except Exception as exc:
        _logger.error("대화 저장 실패: %s", type(exc).__name__)
        return False
```

`create_app` — `read_conn` 오픈 블록 뒤:

```python
    try:
        chat_store = ChatStore(config.data_dir / "chats.db")
        chat_store_error = None
    except Exception as exc:
        # 대화 저장소 장애가 채팅 기능을 볼모로 잡지 않게 — 세션 API만 503, 채팅은 무저장 폴백
        chat_store, chat_store_error = None, type(exc).__name__
        _logger.exception("chats.db를 열 수 없음 — 대화 저장 없이 기동")
```

`state` 리터럴에 `"chat_store": chat_store, "chat_store_error": chat_store_error,`. `_require_db` 아래:

```python
    def _require_chat_store() -> ChatStore:
        store = state.get("chat_store")
        if store is None:
            raise HTTPException(503, f"대화 저장소를 열 수 없습니다: {state.get('chat_store_error')}")
        return store
```

`_shutdown`에 추가:

```python
        store = state.get("chat_store")
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
```

엔드포인트(`/api/usage` 뒤):

```python
    @app.get("/api/chats")
    def chats_list():
        return _require_chat_store().list_sessions()

    @app.post("/api/chats", dependencies=[Depends(local_origin_only)])
    def chats_create(payload: dict | None = None):
        store = _require_chat_store()
        title = (payload or {}).get("title")
        if title is None or title == "":
            title = DEFAULT_TITLE
        if not isinstance(title, str) or len(title) > 200:
            raise HTTPException(400, "title은 200자 이하 문자열이어야 합니다")
        sid = store.create_session(title)
        return {"id": sid, "title": normalize_title(title)}

    @app.get("/api/chats/{session_id}")
    def chats_get(session_id: int):
        try:
            return _require_chat_store().get_session(session_id)
        except KeyError:
            raise HTTPException(404, "세션을 찾을 수 없습니다")

    @app.delete("/api/chats/{session_id}", dependencies=[Depends(local_origin_only)])
    def chats_delete(session_id: int):
        if not _require_chat_store().delete_session(session_id):
            raise HTTPException(404, "세션을 찾을 수 없습니다")
        return {"ok": True}
```

`/api/chat` 교체:

```python
    @app.post("/api/chat", dependencies=[Depends(local_origin_only)])
    def chat(payload: dict):
        _require_db()
        filters = _validate_filters(payload.get("filters"))  # 400은 answer 계상 전에
        session_id = payload.get("session_id")
        store = None
        if session_id is not None:
            if isinstance(session_id, bool) or not isinstance(session_id, int):
                raise HTTPException(404, "세션을 찾을 수 없습니다")
            store = _require_chat_store()
            try:
                history = store.history(session_id)  # 서버가 이력 구성 — 페이로드 history 무시, 현재 질문 미포함
            except KeyError:
                raise HTTPException(404, "세션을 찾을 수 없습니다")
        else:
            history = payload.get("history", [])
        state["usage"].record("answer")
        question = payload.get("question", "")
        if store is not None:
            store.append(session_id, "user", question, filters=filters)  # 스트림 전에 저장 — 중단돼도 질문은 남는다
            if store.get_title(session_id) == DEFAULT_TITLE:
                store.set_title(session_id, question)

        def raw_search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
            return search.search(state["read_conn"], embedder, query, source_filter=source_filter,
                                 date_from=date_from, date_to=date_to, sender=sender)

        search_fn = _apply_filters(raw_search_fn, filters)
        note = _filters_note(filters)

        def event_stream():
            parts: list[str] = []
            hits: list = []
            attempted = False
            try:
                for ev in state["answerer"].answer_stream(question, history, search_fn, filters_note=note):
                    if ev["type"] == "sources":
                        hits = list(ev["hits"])
                        data = json.dumps([asdict(h) for h in hits], ensure_ascii=False)
                        yield f"event: sources\ndata: {data}\n\n"
                    elif ev["type"] == "error":
                        parts.append("\n⚠️ " + ev["message"])
                        yield f"event: error\ndata: {json.dumps(ev['message'], ensure_ascii=False)}\n\n"
                    else:
                        parts.append(ev["text"])
                        yield f"event: text\ndata: {json.dumps(ev['text'], ensure_ascii=False)}\n\n"
                if store is not None:
                    attempted = True
                    if _save_assistant(store, session_id, parts, hits):
                        yield f"event: saved\ndata: {json.dumps({'session_id': session_id})}\n\n"
                yield "event: done\ndata: {}\n\n"
            finally:
                # 클라이언트 중단(GeneratorExit)·답변기 예외 — 부분 답변이라도 보존. finally에서 yield 금지.
                if store is not None and not attempted:
                    _save_assistant(store, session_id, parts, hits)

        return StreamingResponse(event_stream(), media_type="text/event-stream")
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_web.py tests/test_llm.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/app.py src/llmsearch/llm.py tests/test_web.py
git commit -m "feat: 대화 세션 API·/api/chat 세션 통합 — 서버 이력·user 선저장·assistant finally 저장·saved 이벤트 (스펙 M8 §3)"
```

---

### Task 3: 내보내기 API + `chat.export_to_notes`

**Files:**
- Modify: `src/llmsearch/config.py`, `src/llmsearch/web/app.py`, `config.example.yaml`, `README.md`
- Test: `tests/test_config.py`, `tests/test_web.py`, `tests/test_rebuild.py` (추가)

**Interfaces:**
- Consumes: Task 1 `export_markdown`/`get_title`, Task 2 `_require_chat_store`
- Produces: `Config.export_to_notes: bool = False`, `Config.exports_dir`(`data_dir/"exports"`); `_export_slug(title) -> str`; `POST /api/chats/{id}/export` → `{"ok","path"}`; notes 동기화가 `export_to_notes`면 exports 포함. Task 4 UI가 export 호출.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_config.py` 끝:

```python
def test_export_to_notes_loaded_and_default(tmp_path):
    from llmsearch.config import Config, load_config

    p = tmp_path / "config.yaml"
    p.write_text("data_dir: /tmp/x\nchat:\n  export_to_notes: true\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.export_to_notes is True and cfg.exports_dir == cfg.data_dir / "exports"
    p.write_text("data_dir: /tmp/x\nchat:\n", encoding="utf-8")
    assert load_config(p).export_to_notes is False
    assert Config(data_dir=tmp_path).export_to_notes is False
```

`tests/test_web.py` 끝:

```python
def test_export_slug_rules():
    from llmsearch.web.app import _export_slug

    assert _export_slug("프로젝트A 킥오프") == "프로젝트A_킥오프"
    assert _export_slug("../../etc/passwd") == "etc_passwd" or ".." not in _export_slug("../../etc/passwd")
    assert "/" not in _export_slug("a/b\\c:d*e?f") and ".." not in _export_slug("..")
    assert _export_slug("") == "chat" and _export_slug("   ") == "chat"
    assert len(_export_slug("가" * 100)) <= 40


def test_chat_export_deterministic_and_overwrites(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    sid = client.post("/api/chats", json={"title": "내보내기 세션/테스트"}).json()["id"]
    client.post("/api/chat", json={"question": "내보내기 첫 질의", "session_id": sid})
    r = client.post(f"/api/chats/{sid}/export", json={})
    assert r.status_code == 200, r.text
    path = Path(r.json()["path"])
    exports = client.app.state.llmsearch["config"].exports_dir
    assert path.parent == exports and path.name == f"chat-{sid}-내보내기_세션_테스트.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# [대화기록] 내보내기 세션/테스트\n> 이 문서는") and "## Q1. 내보내기 첫 질의" in text
    client.post("/api/chat", json={"question": "내보내기 둘째 질의", "session_id": sid})
    assert Path(client.post(f"/api/chats/{sid}/export", json={}).json()["path"]) == path
    assert "## Q2. 내보내기 둘째 질의" in path.read_text(encoding="utf-8")
    assert sorted(p.name for p in exports.iterdir()) == [path.name]  # 재내보내기는 같은 파일, tmp 잔재 없음
    assert client.post("/api/chats/999/export", json={}).status_code == 404
    assert client.post(f"/api/chats/{sid}/export", json={}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_exports_indexed_as_notes_when_enabled(tmp_path: Path):
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("# 메모", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], export_to_notes=True)
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(), answerer=FakeAnswerer(), enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.post("/api/sync/notes")
    sid = client.post("/api/chats", json={}).json()["id"]
    client.post("/api/chat", json={"question": "노트 인덱싱 질의", "session_id": sid})
    client.post(f"/api/chats/{sid}/export", json={})
    assert client.post("/api/sync/notes").json()["indexed"] == 1
    titles = {r[0] for r in app.state.llmsearch["read_conn"].execute("SELECT title FROM documents WHERE source_type='notes'")}
    assert any(t.startswith("[대화기록] ") for t in titles)
    cfg2 = Config(data_dir=tmp_path / "data2", notes_folders=[notes])  # 기본 false → exports 미포함
    app2 = create_app(cfg2, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(), answerer=FakeAnswerer(), enable_scheduler=False)
    (cfg2.exports_dir).mkdir(parents=True)
    (cfg2.exports_dir / "chat-1-x.md").write_text("# [대화기록] x", encoding="utf-8")
    assert TestClient(app2, base_url="http://127.0.0.1").post("/api/sync/notes").json()["indexed"] == 1  # a.md만
```

`tests/test_rebuild.py` 끝:

```python
def test_rebuild_preserves_chats_db(tmp_path: Path, monkeypatch):
    app, state = make_state(tmp_path, monkeypatch)
    client = client_of(app)
    sid = client.post("/api/chats", json={"title": "보존 확인"}).json()["id"]
    client.post("/api/chat", json={"question": "재구축 보존 질의", "session_id": sid})
    assert client.post("/api/rebuild", json={}).status_code == 200
    wait_resync(state)
    s = client.get(f"/api/chats/{sid}").json()
    assert s["title"] == "재구축 보존 질의" and len(s["messages"]) == 2
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_config.py tests/test_web.py tests/test_rebuild.py -v -k "export or preserves_chats"`
Expected: FAIL — `TypeError: unexpected keyword 'export_to_notes'`, `ImportError: _export_slug`, export 404

- [ ] **Step 3: 구현**

`src/llmsearch/config.py` — 필드 `export_to_notes: bool = False  # 내보낸 대화(md)를 notes로 인덱싱 (스펙 M8 §3)`; 프로퍼티:

```python
    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"
```

`load_config`: `chat = raw.get("chat") or {}` 추가, `Config(...)`에 `export_to_notes=bool(chat.get("export_to_notes", False)),`.

`src/llmsearch/web/app.py` — import `import re`, `from ..summarize import _sanitize_segment`. 모듈:

```python
_SLUG_BAD = re.compile(r"[^0-9A-Za-z가-힣\-_]")


def _export_slug(title: str) -> str:
    """export 파일명 조각 — _sanitize_segment(1계층) 후 허용 문자만, 40자, 빈 값은 chat."""
    return _SLUG_BAD.sub("_", _sanitize_segment(title))[:40].strip("_") or "chat"
```

엔드포인트(`chats_delete` 뒤):

```python
    @app.post("/api/chats/{session_id}/export", dependencies=[Depends(local_origin_only)])
    def chats_export(session_id: int):
        store = _require_chat_store()
        try:
            title = store.get_title(session_id)
            text = store.export_markdown(session_id)
        except KeyError:
            raise HTTPException(404, "세션을 찾을 수 없습니다")
        exports_dir = config.exports_dir
        exports_dir.mkdir(parents=True, exist_ok=True)
        target = exports_dir / f"chat-{session_id}-{_export_slug(title)}.md"  # 세션 단위 결정적 — 재내보내기는 덮어쓰기
        try:
            target.resolve().relative_to(exports_dir.resolve())  # 2계층 검증 (CLAUDE.md)
        except ValueError:
            raise HTTPException(500, "내보내기 실패: 경로 검증")
        try:
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
        except Exception as exc:
            raise HTTPException(500, f"내보내기 실패: {type(exc).__name__}")
        return {"ok": True, "path": str(target)}
```

`run_sync` notes 분기:

```python
            if source == "notes":
                folders = cfg.notes_folders + ([cfg.exports_dir] if cfg.export_to_notes else [])
                result = sync_notes(folders, cfg.exclude, prev, extra_files=[cfg.rules_md_path])
```

`config.example.yaml` 끝에:

```yaml
chat:
  export_to_notes: false   # 설정 탭/채팅 [내보내기]로 저장한 대화 md(data_dir/exports/)를 notes로 인덱싱
```

`README.md` 비용 통제 절 앞에 "## 대화 저장·내보내기" 절: 대화는 `data_dir/chats.db`에 자동 저장(재구축 무관), 채팅 탭 세션 목록에서 복원·삭제, [내보내기]는 `data_dir/exports/chat-<id>-<제목>.md`(재내보내기는 덮어쓰기, 첫 줄 `[대화기록]` 표식), `chat.export_to_notes: true`면 검색 대상.

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_config.py tests/test_web.py tests/test_rebuild.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/config.py src/llmsearch/web/app.py config.example.yaml README.md tests/test_config.py tests/test_web.py tests/test_rebuild.py
git commit -m "feat: 대화 내보내기 — chat-<id>-<slug>.md 결정적 저장·[대화기록] 고지·export_to_notes 옵션 (스펙 M8 §3)"
```

---

### Task 4: UI — 세션 목록·복원·자동 생성·미리보기 dialog

**Files:**
- Modify: `src/llmsearch/web/static/index.html`
- Test: `tests/test_web.py` (추가 1건)

**Interfaces:**
- Consumes: Task 2·3 엔드포인트, SSE `saved`
- Produces: `#sessionBar`(`#sessionSelect`, `#newChatBtn`, `#deleteChatBtn`, `#exportChatBtn`, `#chatNote`), `<dialog id="preview">`(`#previewTitle`, `#previewMeta`, `#previewBody`, `#previewClose`), JS `loadSessions/newChat/selectSession/deleteChat/exportChat/renderSources/previewHit`, 전역 `sessionId`. Task 5 E2E가 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_session_ui_in_index(tmp_path: Path):
    html = make_app(tmp_path).get("/").text
    for needle in ('id="sessionSelect"', 'id="newChatBtn"', 'id="exportChatBtn"', '<dialog id="preview"',
                   'id="previewBody"', "renderSources(", "loadSessions();"):
        assert needle in html, needle
```

- [ ] **Step 2: 실패 확인** — `./.venv/bin/pytest tests/test_web.py -v -k session_ui` → FAIL

- [ ] **Step 3: 구현**

`index.html` 채팅 탭 `<div id="messages">` 앞에:

```html
  <div id="sessionBar">
    <select id="sessionSelect" onchange="selectSession()"><option value="">— 새 대화 —</option></select>
    <button id="newChatBtn" onclick="newChat()">새 대화</button>
    <button id="deleteChatBtn" onclick="deleteChat()">삭제</button>
    <button id="exportChatBtn" onclick="exportChat()">내보내기</button>
    <span id="chatNote"></span>
  </div>
```

`</body>` 앞(스크립트 밖)에:

```html
<dialog id="preview" style="max-width:800px">
  <h3 id="previewTitle"></h3><small id="previewMeta"></small>
  <pre id="previewBody" style="white-space:pre-wrap;max-height:60vh;overflow:auto"></pre>
  <button id="previewClose" onclick="document.getElementById('preview').close()">닫기</button>
</dialog>
```

`show()`의 chat 분기 추가: `if (id === 'chat') loadSessions();`. JS(`ask` 앞):

```js
let sessionId = null;
function opt(value, text) { const o = document.createElement('option'); o.value = value; o.textContent = text; return o; }
async function loadSessions() {
  const sel = document.getElementById('sessionSelect');
  const r = await fetch('/api/chats');
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    document.getElementById('chatNote').textContent = '대화 저장 불가: ' + (d.detail || ('HTTP ' + r.status));
    return;
  }
  const list = await r.json();
  sel.replaceChildren(opt('', '— 새 대화 —'), ...list.map(s => opt(String(s.id), s.title)));
  sel.value = sessionId === null ? '' : String(sessionId);
}
function clearConversation() { document.getElementById('messages').replaceChildren(); history.length = 0; }
function newChat() { sessionId = null; clearConversation(); document.getElementById('sessionSelect').value = ''; }
async function selectSession() {
  const v = document.getElementById('sessionSelect').value;
  if (!v) { newChat(); return; }
  const r = await fetch('/api/chats/' + encodeURIComponent(v));
  if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.detail || '세션을 불러올 수 없습니다'); return; }
  const s = await r.json();
  sessionId = s.id; clearConversation();
  const box = document.getElementById('messages');
  for (const m of s.messages) {
    if (m.role === 'user') {
      const q = document.createElement('div'); q.className = 'msg-q'; q.textContent = 'Q. ' + m.content; box.appendChild(q);
      const label = m.filters ? filtersLabel(m.filters) : '';
      if (label) { const n = document.createElement('div'); n.className = 'filters-note'; n.textContent = label; box.appendChild(n); }
    } else {
      const a = document.createElement('div'); a.className = 'msg-a'; a.textContent = m.content; box.appendChild(a);
      renderSources(a, m.sources || []);
    }
  }
  box.scrollTop = box.scrollHeight;
}
async function deleteChat() {
  if (sessionId === null || !confirm('이 대화를 삭제할까요?')) return;
  const r = await fetch('/api/chats/' + sessionId, {method: 'DELETE'});
  if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.detail || '삭제 실패'); return; }
  newChat(); loadSessions();
}
async function exportChat() {
  if (sessionId === null) { alert('저장된 대화가 없습니다 — 질문을 먼저 보내세요'); return; }
  const r = await fetch('/api/chats/' + sessionId + '/export', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const d = await r.json().catch(() => ({}));
  alert(r.ok ? '내보내기 완료: ' + d.path : (d.detail || '내보내기 실패'));
}
function renderSources(answerDiv, hits) {
  answerDiv._hits = hits;  // 미리보기용 — excerpt는 SSE/세션 응답에 이미 포함
  hits.forEach((h, i) => {
    const lock = h.content_indexed ? '' : ' 🔒';
    const resum = h.source_type === 'local_docs'
      ? ` <button onclick="resummarize(this.dataset.s)" data-s="${esc(h.source_id)}">재요약</button>` : '';
    answerDiv.insertAdjacentHTML('beforeend',
      `<div class="src">📄 ${esc(h.title)}${lock} <small>(${esc(h.source_type)} · ${esc(h.updated_at)})</small><br>` +
      `${h.snippet ? `<div class="snip">${esc(h.snippet)}</div>` : ''}` +
      `<code>${esc(h.url_or_path)}</code> ` +
      `<button onclick="openItem(this.dataset.p)" data-p="${esc(h.url_or_path)}">열기</button> ` +
      `<button onclick="previewHit(this)" data-i="${i}">미리보기</button>${resum}</div>`);
  });
}
function previewHit(btn) {
  const h = btn.closest('.msg-a')._hits[Number(btn.dataset.i)];
  document.getElementById('previewTitle').textContent = h.title;
  document.getElementById('previewMeta').textContent = `${h.source_type} · ${h.updated_at} · ${h.url_or_path}`;
  document.getElementById('previewBody').textContent = h.excerpt || '(본문 없음)';
  document.getElementById('preview').showModal();
}
```

`ask()` 수정: fetch 앞에 세션 자동 생성 + 페이로드 분기:

```js
  if (sessionId === null) {
    const cr = await fetch('/api/chats', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    if (cr.ok) { sessionId = (await cr.json()).id; document.getElementById('chatNote').textContent = ''; }
    else { const d = await cr.json().catch(() => ({})); document.getElementById('chatNote').textContent = '대화 저장 불가: ' + (d.detail || ('HTTP ' + cr.status)); }
  }
  const body = sessionId === null ? {question: q, history, filters} : {question: q, session_id: sessionId, filters};
```

fetch의 `body: JSON.stringify(body)`. 스트림 루프: `sources` 분기를 `renderSources(answerDiv, JSON.parse(data));`로 교체(기존 인라인 카드 템플릿 삭제 — 중복 제거), `else if (ev === 'saved') loadSessions();` 추가. 마지막 `history.push(...)`는 `if (sessionId === null) history.push(...)`. 스크립트 말미 `loadStatus();` 뒤에 `loadSessions();`.

- [ ] **Step 4: 통과 확인** — `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/static/index.html tests/test_web.py
git commit -m "feat: 채팅 세션 UI — 목록·복원·자동 생성·삭제·내보내기·출처 미리보기 dialog (스펙 M8 §4)"
```

---

### Task 5: E2E + HANDOFF + 상위 스펙 §13

**Files:**
- Modify: `tools/e2e/verify.py`(9.11 마지막 체크 `골든 미스 표시` 뒤, `# 10.` 앞), `docs/HANDOFF.md`, `docs/superpowers/specs/2026-08-17-llmsearch-design.md`(§13 트리에 `chats.db`·`exports/`)

- [ ] **Step 1: 시나리오 삽입**

```python
    # 9.12 M8 — 세션 자동 생성·복원(새로고침)·미리보기·내보내기 (스펙 M8)
    page.click("nav >> text=채팅")
    page.click("#newChatBtn")
    opts_before = page.locator("#sessionSelect option").count()
    page.fill("#question", "프로젝트A 세션 저장 확인 질의")
    before_cards = page.locator(".src").count()
    page.click("form >> text=검색")
    page.wait_for_function(f"document.querySelectorAll('.src').length > {before_cards}", timeout=10000)
    page.wait_for_function(f"document.querySelectorAll('#sessionSelect option').length > {opts_before}", timeout=10000)
    sel = page.locator("#sessionSelect")
    check("세션 자동 생성", sel.locator("option").count() == opts_before + 1, f"{opts_before}→{sel.locator('option').count()}")
    check("세션 제목=질문", sel.locator("option:checked").inner_text().strip() == "프로젝트A 세션 저장 확인 질의",
          sel.locator("option:checked").inner_text())
    saved_id = sel.input_value()
    page.reload()
    page.wait_for_function("document.querySelectorAll('#sessionSelect option').length >= 2", timeout=10000)
    page.select_option("#sessionSelect", saved_id)
    page.wait_for_selector(".msg-a", timeout=10000)
    check("세션 복원: 질문·답변", page.locator(".msg-a").count() >= 1
          and "프로젝트A 세션 저장 확인 질의" in page.locator("#messages").inner_text())
    check("세션 복원: 출처 카드", page.locator(".src").count() >= 1)
    page.locator(".src button", has_text="미리보기").first.click()
    page.wait_for_selector("#preview[open]", timeout=5000)
    check("미리보기 본문", page.locator("#previewBody").inner_text().strip() != "")
    page.click("#previewClose")
    dialogs.clear()
    page.click("#exportChatBtn")
    page.wait_for_timeout(500)
    exports = list((DATA / "data" / "exports").glob("chat-*.md"))
    check("내보내기 alert", any("내보내기 완료" in m for m in dialogs), " / ".join(m[:40] for m in dialogs))
    body_md = exports[0].read_text(encoding="utf-8") if exports else ""
    check("내보내기 파일", len(exports) == 1 and body_md.startswith("# [대화기록]") and "프로젝트A 세션 저장 확인 질의" in body_md, str(exports))
    page.click("#newChatBtn")  # 이후 단계(10단계 UI 채팅)가 새 세션에서 시작하게
```

- [ ] **Step 2: 실행 검증** — 데모 서버 기동 → `./.venv/bin/python tools/e2e/verify.py` → `총 81건 전부 PASS` (73 + 8). 예산: 채팅 1회(+2) ≈ 38 < 50. `page.on("dialog")`는 reload 후 유지된다.

- [ ] **Step 3: 문서** — HANDOFF §1 표 `| M8 채팅 UX | 🔀 브랜치 완료(머지 시 ✅로) | 세션 저장/복원(chats.db)·내보내기(export_to_notes)·출처 미리보기 |`, 기준 테스트 수/E2E 81 갱신, §3 다음 작업 = M9(로컬 임베딩 스파이크), §5 문서 지도에 M8 스펙·계획, §6 수동 게이트 "M8: 실 Claude 세션에서 후속 질문이 이전 맥락을 잇는지, 새로고침 복원, 내보내기 md 확인". 상위 스펙 §13 트리에 `├─ chats.db   # 대화 세션 (M8 — 인덱스와 분리, rebuild 무관)`, `├─ exports/   # 내보낸 대화 md (chat.export_to_notes로 인덱싱 가능)` 추가.

- [ ] **Step 4: Commit**

```bash
git add tools/e2e/verify.py docs/HANDOFF.md docs/superpowers/specs/2026-08-17-llmsearch-design.md
git commit -m "test: E2E 확장 — M8 세션 저장/복원·미리보기·내보내기 시나리오 + 문서 (전 항목 PASS)"
```

---

## M8 수동 체크리스트 (실환경 — 머지 후 사용자 확인)

1. 실 Claude로 질문 2회 → 두 번째 답변이 첫 질문 맥락을 잇는지(서버 이력)
2. 답변 스트리밍 중 새로고침 → 세션 복원 시 부분 답변이 남아 있는지
3. [내보내기] md 열어 `[대화기록]` 고지·출처 목록 확인, `chat.export_to_notes: true` 후 동기화 → 검색에 잡히는지
