# llmsearch M2 구현 계획 (Outlook 메일·일정)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스펙 M2 — Outlook 메일·일정 커넥터(COM 전용 워커 스레드, 재개 가능한 콜드스타트, 인용 절단, 삭제 대조)와 M1 이월 수정 1건. 완료 시 메일·일정이 통합 검색·채팅에 합류한다.

**Architecture:** COM 의존을 `OutlookClient` 프로토콜(순수 dict 계약) 뒤로 격리한다. 커넥터(`outlook_mail`/`outlook_cal`)는 프로토콜만 소비하므로 WSL에서 Fake로 완전 테스트되고, 실제 COM 호출은 `ComWorker`(CoInitialize된 전용 스레드, 스펙 §5 P0) 위의 `ThreadedOutlookClient`가 담당한다. pywin32는 Windows 실행 시에만 지연 import.

**Tech Stack:** 기존 M1 스택 + pywin32(선택 의존성 `[win]`, Windows 전용). 테스트는 전부 Fake — pywin32 불필요.

**스펙:** `docs/superpowers/specs/2026-08-17-llmsearch-design.md` §4 M2, §7.4~7.5, §5 원칙. **M1 코드가 기준선** — plan 문서가 아니라 현재 `src/llmsearch/`의 실제 코드가 정본이다 (M1 수정 라운드로 plan 참조 코드와 일부 다름).

## Global Constraints

- 개발·테스트는 WSL에서 동작: **pywin32/pythoncom/win32com은 어떤 모듈에서도 최상위 import 금지** (함수 내부 지연 import만). 전체 테스트는 API 키·pywin32 없이 통과해야 한다
- COM 접근은 `ComWorker` 스레드에서만 (스펙 §5 P0: FastAPI 스레드풀에서 COM 직접 호출 금지)
- 메일·일정은 파일 미러 없음 — 인덱스에만 (스펙 §3 결정)
- 메일 증분: `received_at` 커서, 콜드스타트는 배치(기본 200통)로 중단·재개 가능 (스펙 §7.4 P0)
- 반복 일정은 조회 기간 내로만 전개 (스펙 §7.5)
- 쓰기는 기존 `run_sync`+`sync_lock` 경로 유지; 이벤트/스케줄러 코드는 M1 패턴 그대로 따른다
- 커밋은 태스크마다 conventional commits; TDD 순서(실패 테스트 → 구현 → 통과) 준수

## 파일 구조 (M2 추가/수정)

```
src/llmsearch/
├─ mailtext.py                    # [NEW] 인용 체인·서명 절단 순수 함수
├─ outlook/
│  ├─ __init__.py                 # [NEW]
│  ├─ client.py                   # [NEW] OutlookClient 프로토콜 + FakeOutlookClient + dict 계약 정의
│  ├─ com_worker.py               # [NEW] ComWorker — CoInitialize 전용 스레드 + submit()
│  └─ com_client.py               # [NEW] ComOutlookClient(실 COM) + ThreadedOutlookClient(워커 래핑)
├─ connectors/
│  ├─ outlook_mail.py             # [NEW] 메일 커넥터 (커서·배치·절단·제외·삭제 대조)
│  ├─ outlook_cal.py              # [NEW] 일정 커넥터 (기간 창·반복 전개 결과 수용)
│  └─ local_docs.py               # [MOD] Task 1: stat() 실패 시 재시도 센티널
├─ config.py                      # [MOD] mail/cal 설정 키 추가
└─ web/app.py                     # [MOD] SOURCES 확장 + Outlook 배선 + 진행 상태
scripts/check_outlook.py          # [NEW] Windows 수동 점검 스크립트 (COM 스모크)
pyproject.toml                    # [MOD] [project.optional-dependencies] win = ["pywin32>=306"]
README.md                        # [MOD] M2 실행 안내
tests/test_mailtext.py, test_outlook_client.py, test_com_worker.py,
tests/test_outlook_mail.py, test_outlook_cal.py, test_web_outlook.py   # [NEW]
```

---

### Task 1: M1 이월 수정 — local_docs stat() 실패 재시도 센티널

**Files:**
- Modify: `src/llmsearch/connectors/local_docs.py` (stat의 `except OSError: continue` 부분)
- Test: `tests/test_local_docs.py` (추가)

**Interfaces:**
- Consumes: 기존 `_RETRY_SENTINEL`(요약 실패 경로에 이미 존재, `[0.0, 0]`)
- Produces: 동작 변경만 — stat 실패한 기수집 파일이 deleted로 오판되지 않음

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_local_docs.py`에 추가

```python
def test_stat_failure_previously_synced_not_deleted(tmp_path: Path, patch_extract, monkeypatch):
    """stat() 일시 실패(파일 잠김 등) 시 기수집 파일이 삭제로 오판되면 안 된다."""
    docs = tmp_path / "docs"; docs.mkdir()
    f = docs / "킥오프.pptx"; f.write_bytes(b"v1")
    r1 = run(tmp_path, docs)
    sid = r1.documents[0].source_id
    summary = Path(r1.documents[0].extra["summary_path"])

    real_stat = Path.stat
    def flaky_stat(self, **kw):
        if self.name == "킥오프.pptx":
            raise PermissionError("locked")
        return real_stat(self, **kw)
    monkeypatch.setattr(Path, "stat", flaky_stat)
    r2 = run(tmp_path, docs, state=r1.state,
             prior={sid: (r1.documents[0].extra["para_path"], str(summary))})
    assert sid not in r2.deleted_ids
    assert summary.exists()
    monkeypatch.undo()
    # 복구 후 재동기화되어야 함 (센티널 시그니처 불일치 → 재처리)
    r3 = run(tmp_path, docs, state=r2.state,
             prior={sid: (r1.documents[0].extra["para_path"], str(summary))})
    assert any(d.source_id == sid for d in r3.documents)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_local_docs.py::test_stat_failure_previously_synced_not_deleted -v`
Expected: FAIL — 현행 코드는 stat 실패 시 `seen`에서 빠져 `deleted_ids`에 포함됨

- [ ] **Step 3: 구현** — `local_docs.py`의 stat 예외 처리에서, 실패 시 `seen[sid] = list(_RETRY_SENTINEL)` 기록 후 `continue` (요약 실패 경로와 동일 패턴으로 통일)

```python
            try:
                st = path.stat()
            except OSError:
                seen[sid] = list(_RETRY_SENTINEL)  # 삭제 오판 방지 + 다음 라운드 재시도
                continue
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_local_docs.py -v` → 전부 PASS, 이어서 전체 스위트 `./.venv/bin/python -m pytest tests/ -q` → PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/connectors/local_docs.py tests/test_local_docs.py
git commit -m "fix: local_docs stat 일시 실패 시 삭제 오판 방지(재시도 센티널)"
```

---

### Task 2: mailtext — 인용 체인·서명 절단 순수 함수

**Files:**
- Create: `src/llmsearch/mailtext.py`
- Test: `tests/test_mailtext.py`

**Interfaces:**
- Produces: `clean_mail_body(body: str, max_chars: int = 20000) -> str` — 인용 구분선 이후 절단 + 서명 제거 + 길이 상한 (스펙 §7.4 P1)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_mailtext.py`

```python
from llmsearch.mailtext import clean_mail_body


def test_cuts_original_message_marker():
    body = "회신 본문입니다.\n\n-----Original Message-----\nFrom: a@b.com\n이전 메일 전문"
    out = clean_mail_body(body)
    assert "회신 본문" in out and "이전 메일 전문" not in out


def test_cuts_korean_reply_header():
    body = "답장 내용.\n\n보낸 사람: 김철수 <kim@corp.com>\n받는 사람: 나\n이전 내용"
    out = clean_mail_body(body)
    assert "답장 내용" in out and "이전 내용" not in out


def test_cuts_gmail_style_quote():
    body = "본문.\n\n2026년 8월 1일 (금) 오전 10:00, 김철수님이 작성:\n> 인용문"
    out = clean_mail_body(body)
    assert "본문" in out and "인용문" not in out


def test_strips_signature():
    body = "본문 내용.\n\n--\n김철수 드림\n01x-xxxx-xxxx"
    out = clean_mail_body(body)
    assert "본문 내용" in out and "드림" not in out


def test_keeps_body_without_markers():
    assert clean_mail_body("마커 없는 짧은 본문") == "마커 없는 짧은 본문"


def test_length_cap():
    assert len(clean_mail_body("가" * 50000, max_chars=1000)) <= 1000


def test_marker_at_start_keeps_nothing_but_no_crash():
    out = clean_mail_body("-----Original Message-----\n전부 인용")
    assert out == ""
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_mailtext.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/mailtext.py`

```python
from __future__ import annotations

import re

# 인용 시작 마커 — 이 줄부터 끝까지 절단 (스펙 §7.4: 답장 스레드 중복 인덱싱 방지)
_QUOTE_MARKERS = [
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^-{3,}\s*원본 메일\s*-{3,}"),
    re.compile(r"^From:\s?.+@"),
    re.compile(r"^보낸 사람\s?:"),
    re.compile(r"^발신\s?:"),
    re.compile(r"^On .+ wrote:\s*$"),
    re.compile(r"^\d{4}년 .+님이 작성:\s*$"),
    re.compile(r"^>"),  # 인용 접두 줄
]
_SIGNATURE_MARKER = re.compile(r"^--\s*$")


def clean_mail_body(body: str, max_chars: int = 20000) -> str:
    lines = body.splitlines()
    cut = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _SIGNATURE_MARKER.match(stripped):
            cut = i
            break
        if any(m.match(stripped) for m in _QUOTE_MARKERS):
            cut = i
            break
    cleaned = "\n".join(lines[:cut]).strip()
    return cleaned[:max_chars]
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_mailtext.py -v` → PASS (7건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/mailtext.py tests/test_mailtext.py
git commit -m "feat: 메일 인용 체인·서명 절단(clean_mail_body)"
```

---

### Task 3: OutlookClient 프로토콜 + FakeOutlookClient

**Files:**
- Create: `src/llmsearch/outlook/__init__.py` (빈 파일), `src/llmsearch/outlook/client.py`
- Test: `tests/test_outlook_client.py`

**Interfaces:**
- Produces (이후 모든 태스크가 이 계약에 의존):
  - 메일 dict: `{"entry_id": str, "subject": str, "body": str, "sender_name": str, "sender_email": str, "received_at": datetime, "folder": str}`
  - 일정 dict: `{"entry_id": str, "subject": str, "body": str, "location": str, "start": datetime, "end": datetime, "attendees": str}` (반복 일정은 전개된 각 회차가 개별 dict, entry_id는 마스터와 동일할 수 있음 — 회차 구분은 start로)
  - `class OutlookClient(Protocol):`
    - `def is_available(self) -> bool`
    - `def list_mail(self, folder: str, since: datetime, until: datetime | None = None, limit: int | None = None) -> list[dict]` — `received_at` 오름차순, `since` 초과(exclusive)만
    - `def list_mail_ids(self, folder: str, since: datetime) -> set[str]` — 삭제 대조용
    - `def list_appointments(self, window_start: datetime, window_end: datetime) -> list[dict]` — 기간과 겹치는 회차 전부(반복 전개 포함)
    - `def open_item(self, entry_id: str) -> None` — 해당 항목을 Outlook 창으로 표시 (스펙 §7.4 "클릭 시 Outlook에서 열기")
  - `FakeOutlookClient(mails: dict[str, list[dict]] = None, appointments: list[dict] = None, available: bool = True)` — 폴더명→메일 목록; 프로토콜 시맨틱(정렬·since exclusive·limit) 그대로 구현; `open_item` 호출은 `self.opened: list[str]`에 기록

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_outlook_client.py`

```python
from datetime import datetime

from llmsearch.outlook.client import FakeOutlookClient


def mail(eid, ts, subject="제목"):
    return {"entry_id": eid, "subject": subject, "body": "본문", "sender_name": "김철수",
            "sender_email": "kim@corp.com", "received_at": ts, "folder": "inbox"}


def test_list_mail_sorted_and_since_exclusive():
    c = FakeOutlookClient(mails={"inbox": [
        mail("b", datetime(2026, 8, 2)), mail("a", datetime(2026, 8, 1)), mail("c", datetime(2026, 8, 3)),
    ]})
    out = c.list_mail("inbox", since=datetime(2026, 8, 1))
    assert [m["entry_id"] for m in out] == ["b", "c"]  # since와 같은 시각은 제외, 오름차순


def test_list_mail_limit():
    c = FakeOutlookClient(mails={"inbox": [mail(str(i), datetime(2026, 8, 1, i)) for i in range(5)]})
    out = c.list_mail("inbox", since=datetime(2026, 7, 1), limit=2)
    assert len(out) == 2 and out[0]["entry_id"] == "0"


def test_list_mail_ids():
    c = FakeOutlookClient(mails={"inbox": [mail("a", datetime(2026, 8, 1)), mail("b", datetime(2026, 8, 2))]})
    assert c.list_mail_ids("inbox", since=datetime(2026, 7, 31)) == {"a", "b"}
    assert c.list_mail_ids("inbox", since=datetime(2026, 8, 1)) == {"b"}


def test_unknown_folder_empty():
    assert FakeOutlookClient().list_mail("없는폴더", since=datetime(2026, 1, 1)) == []


def test_list_appointments_window_overlap():
    c = FakeOutlookClient(appointments=[
        {"entry_id": "e1", "subject": "회의", "body": "", "location": "회의실",
         "start": datetime(2026, 8, 10, 10), "end": datetime(2026, 8, 10, 11), "attendees": "나"},
        {"entry_id": "e2", "subject": "과거", "body": "", "location": "",
         "start": datetime(2026, 1, 1, 10), "end": datetime(2026, 1, 1, 11), "attendees": ""},
    ])
    out = c.list_appointments(datetime(2026, 8, 1), datetime(2026, 9, 1))
    assert [a["entry_id"] for a in out] == ["e1"]


def test_availability_flag():
    assert FakeOutlookClient(available=False).is_available() is False


def test_open_item_recorded():
    c = FakeOutlookClient()
    c.open_item("abc")
    assert c.opened == ["abc"]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_outlook_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/outlook/client.py`

```python
"""Outlook 접근 계약.

메일 dict: entry_id, subject, body, sender_name, sender_email, received_at(datetime), folder
일정 dict: entry_id, subject, body, location, start(datetime), end(datetime), attendees
구현체는 이 dict 계약과 정렬/필터 시맨틱을 지켜야 한다. COM 세부는 com_client.py에만 존재한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol


class OutlookClient(Protocol):
    def is_available(self) -> bool: ...

    def list_mail(self, folder: str, since: datetime, until: datetime | None = None,
                  limit: int | None = None) -> list[dict]: ...

    def list_mail_ids(self, folder: str, since: datetime) -> set[str]: ...

    def list_appointments(self, window_start: datetime, window_end: datetime) -> list[dict]: ...

    def open_item(self, entry_id: str) -> None: ...


class FakeOutlookClient:
    """테스트·오프라인 개발용 — 프로토콜 시맨틱(received_at 오름차순, since exclusive)을 그대로 구현."""

    def __init__(self, mails: dict[str, list[dict]] | None = None,
                 appointments: list[dict] | None = None, available: bool = True):
        self.mails = mails or {}
        self.appointments = appointments or []
        self.available = available
        self.opened: list[str] = []

    def is_available(self) -> bool:
        return self.available

    def list_mail(self, folder: str, since: datetime, until: datetime | None = None,
                  limit: int | None = None) -> list[dict]:
        items = [m for m in self.mails.get(folder, []) if m["received_at"] > since]
        if until is not None:
            items = [m for m in items if m["received_at"] <= until]
        items.sort(key=lambda m: m["received_at"])
        return items[:limit] if limit is not None else items

    def list_mail_ids(self, folder: str, since: datetime) -> set[str]:
        return {m["entry_id"] for m in self.mails.get(folder, []) if m["received_at"] > since}

    def list_appointments(self, window_start: datetime, window_end: datetime) -> list[dict]:
        return sorted(
            (a for a in self.appointments if a["end"] > window_start and a["start"] < window_end),
            key=lambda a: a["start"],
        )

    def open_item(self, entry_id: str) -> None:
        self.opened.append(entry_id)
```

`src/llmsearch/outlook/__init__.py`는 빈 파일.

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_outlook_client.py -v` → PASS (7건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/outlook tests/test_outlook_client.py
git commit -m "feat: OutlookClient 프로토콜 + FakeOutlookClient"
```

---

### Task 4: ComWorker — COM 전용 워커 스레드

**Files:**
- Create: `src/llmsearch/outlook/com_worker.py`
- Test: `tests/test_com_worker.py`

**Interfaces:**
- Produces:
  - `class ComWorker:` — `submit(fn, *args, **kwargs) -> Any` (워커 스레드에서 실행, 결과 반환, 예외 전파), `shutdown()` (스레드 종료)
  - 스레드 시작 시 `_com_initialize()`, 종료 시 `_com_uninitialize()` 호출 — pythoncom이 없으면(WSL) 자동 no-op → 워커 자체는 WSL에서 테스트 가능
- Consumes: 없음 (표준 라이브러리만)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_com_worker.py`

```python
import threading

import pytest
from llmsearch.outlook.com_worker import ComWorker


def test_submit_runs_on_single_dedicated_thread():
    w = ComWorker()
    try:
        t1 = w.submit(lambda: threading.current_thread().name)
        t2 = w.submit(lambda: threading.current_thread().name)
        assert t1 == t2 == "com-worker"
        assert t1 != threading.current_thread().name
    finally:
        w.shutdown()


def test_submit_returns_value_and_propagates_exception():
    w = ComWorker()
    try:
        assert w.submit(lambda a, b: a + b, 1, 2) == 3
        with pytest.raises(ValueError, match="boom"):
            w.submit(lambda: (_ for _ in ()).throw(ValueError("boom")))
        # 예외 후에도 워커는 살아 있어야 함
        assert w.submit(lambda: "ok") == "ok"
    finally:
        w.shutdown()


def test_shutdown_then_submit_raises():
    w = ComWorker()
    w.shutdown()
    with pytest.raises(RuntimeError):
        w.submit(lambda: 1)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_com_worker.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/outlook/com_worker.py`

```python
"""COM 전용 워커 스레드 (스펙 §5 P0).

win32com은 COM 아파트먼트 초기화가 필요하고 스레드 친화성이 있다 — 모든 COM 접근은
이 워커의 단일 스레드에서만 일어나야 한다. FastAPI 스레드풀에서 직접 호출 금지.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable


def _com_initialize() -> None:
    try:
        import pythoncom  # Windows 전용 — WSL에서는 no-op

        pythoncom.CoInitialize()
    except ImportError:
        pass


def _com_uninitialize() -> None:
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except ImportError:
        pass


_STOP = object()


class ComWorker:
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="com-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        _com_initialize()
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                fn, args, kwargs, done, box = item
                try:
                    box["result"] = fn(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 — 호출자에게 그대로 전파
                    box["error"] = exc
                done.set()
        finally:
            _com_uninitialize()

    def submit(self, fn: Callable, *args, **kwargs) -> Any:
        if self._closed:
            raise RuntimeError("ComWorker is shut down")
        done = threading.Event()
        box: dict = {}
        self._queue.put((fn, args, kwargs, done, box))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def shutdown(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put(_STOP)
            self._thread.join(timeout=5)
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_com_worker.py -v` → PASS (3건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/outlook/com_worker.py tests/test_com_worker.py
git commit -m "feat: ComWorker — CoInitialize 전용 스레드 + submit"
```

---

### Task 5: outlook_mail 커넥터 — 커서·배치 콜드스타트·절단·제외·삭제 대조

**Files:**
- Create: `src/llmsearch/connectors/outlook_mail.py`
- Test: `tests/test_outlook_mail.py`

**Interfaces:**
- Consumes: `OutlookClient` 프로토콜(Task 3), `clean_mail_body`(Task 2), `rules.is_excluded`, `models.Document/SyncResult`
- Produces:
  - `sync_outlook_mail(client: OutlookClient, folders: list[str], since_days: int, excludes: list[str], state: dict, batch_size: int = 200, now: datetime | None = None) -> SyncResult`
  - `state` 형식: `{"cursor": {folder: iso_ts}, "known_ids": {folder: [entry_id,...]}, "last_reconcile": iso_ts | None}`
  - 시맨틱: 폴더별 커서 이후 메일을 `batch_size`씩 수집(콜드스타트 재개 가능, 스펙 §7.4 P0). **동시각 경계 보호**: 배치가 가득 찼을 때(fetch == batch_size) 마지막 `received_at`과 같은 꼬리 동시각 그룹은 이번 라운드에서 제외(트림)하고 다음 라운드에 통째로 처리 — 커서가 exclusive(`>`)여도 유실 없음. 트림하면 배치가 비는 경우(전부 동시각 ≥ batch_size)만 전량 처리 후 진행(문서화된 병리적 한계). 모든 폴더가 배치 한도에 안 걸린 라운드(정상 상태)이고 `last_reconcile`이 24시간 이전이면 `list_mail_ids`로 삭제 대조 수행. Document: `source_id=entry_id`, `url_or_path=f"outlook:{entry_id}"`, `title=subject`, `updated_at=received_at`, `extra={"sender": sender_email, "folder": folder}`, 본문은 `clean_mail_body` 적용 + 발신자·날짜 헤더 포함
  - `backlog_hint(state) -> bool` — 직전 라운드가 배치 한도에 걸렸는지(콜드스타트 진행 중 표시용, GUI가 사용)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_outlook_mail.py`

```python
from datetime import datetime, timedelta

from llmsearch.connectors.outlook_mail import backlog_hint, sync_outlook_mail
from llmsearch.outlook.client import FakeOutlookClient

NOW = datetime(2026, 8, 17, 12, 0)


def mail(eid, ts, sender="kim@corp.com", folder="inbox", body="본문"):
    return {"entry_id": eid, "subject": f"제목{eid}", "body": body, "sender_name": "김철수",
            "sender_email": sender, "received_at": ts, "folder": folder}


def test_initial_sync_batched_and_resumable():
    """콜드스타트: 배치 단위 재개, 중복·누락 없음. 가득 찬 배치는 꼬리 동시각 그룹을
    트림하므로 유효 배치가 batch_size보다 작을 수 있다 — 라운드를 반복해 전량 수집 확인."""
    mails = [mail(str(i), NOW - timedelta(days=10) + timedelta(hours=i)) for i in range(5)]
    c = FakeOutlookClient(mails={"inbox": mails})
    state: dict = {}
    collected: list[str] = []
    for _ in range(10):  # 충분한 라운드 상한
        r = sync_outlook_mail(c, ["inbox"], since_days=365, excludes=[], state=state,
                              batch_size=2, now=NOW)
        ids = [d.source_id for d in r.documents]
        assert not set(ids) & set(collected)  # 중복 없음
        collected.extend(ids)
        state = r.state
        if not backlog_hint(state) and not ids:
            break
    assert set(collected) == {"0", "1", "2", "3", "4"}  # 누락 없음
    assert backlog_hint(state) is False
    # 첫 라운드는 배치 한도에 걸렸어야 함 (콜드스타트 진행 표시)
    r1 = sync_outlook_mail(FakeOutlookClient(mails={"inbox": mails}), ["inbox"], 365, [], {},
                           batch_size=2, now=NOW)
    assert backlog_hint(r1.state) is True


def test_document_shape_and_body_cleaning():
    body = "핵심 내용\n\n-----Original Message-----\n이전 메일"
    c = FakeOutlookClient(mails={"inbox": [mail("a", NOW - timedelta(days=1), body=body)]})
    r = sync_outlook_mail(c, ["inbox"], 365, [], {}, now=NOW)
    d = r.documents[0]
    assert d.source_type == "outlook_mail"
    assert d.url_or_path == "outlook:a"
    assert d.extra["sender"] == "kim@corp.com"
    assert "핵심 내용" in d.text and "이전 메일" not in d.text
    assert "kim@corp.com" in d.text  # 발신자 헤더 포함


def test_sender_and_folder_excludes():
    c = FakeOutlookClient(mails={
        "inbox": [mail("a", NOW - timedelta(days=1), sender="spam@ads.com"),
                  mail("b", NOW - timedelta(days=1, hours=1))],
        "인사평가": [mail("c", NOW - timedelta(days=1), folder="인사평가")],
    })
    r = sync_outlook_mail(c, ["inbox", "인사평가"], 365,
                          ["sender:*@ads.com", "folder:인사평가"], {}, now=NOW)
    assert {d.source_id for d in r.documents} == {"b"}


def test_reconcile_reports_deleted():
    old = NOW - timedelta(days=2)
    c = FakeOutlookClient(mails={"inbox": [mail("a", old), mail("b", old + timedelta(hours=1))]})
    r1 = sync_outlook_mail(c, ["inbox"], 365, [], {}, now=NOW)
    assert {d.source_id for d in r1.documents} == {"a", "b"}
    del c.mails["inbox"][0]  # a 삭제
    # last_reconcile을 과거로 밀어 대조 강제
    r1.state["last_reconcile"] = (NOW - timedelta(days=2)).isoformat()
    r2 = sync_outlook_mail(c, ["inbox"], 365, [], r1.state, now=NOW)
    assert r2.deleted_ids == ["a"]


def test_unavailable_client_raises():
    import pytest
    c = FakeOutlookClient(available=False)
    with pytest.raises(RuntimeError, match="Outlook"):
        sync_outlook_mail(c, ["inbox"], 365, [], {}, now=NOW)


def test_same_timestamp_at_batch_boundary_not_lost():
    ts1 = NOW - timedelta(days=1)
    ts2 = ts1 + timedelta(minutes=5)
    mails = [mail("a1", ts1), mail("a2", ts1), mail("b1", ts2), mail("b2", ts2)]
    c = FakeOutlookClient(mails={"inbox": mails})
    # batch 3: fetch [a1,a2,b1] 가득참 → 꼬리 동시각(b1) 트림 → a1,a2만 처리
    r1 = sync_outlook_mail(c, ["inbox"], 365, [], {}, batch_size=3, now=NOW)
    assert {d.source_id for d in r1.documents} == {"a1", "a2"}
    # 다음 라운드에 b1,b2 통째로 — 유실·중복 없음
    r2 = sync_outlook_mail(c, ["inbox"], 365, [], r1.state, batch_size=3, now=NOW)
    assert {d.source_id for d in r2.documents} == {"b1", "b2"}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_outlook_mail.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/connectors/outlook_mail.py`

```python
from __future__ import annotations

from datetime import datetime, timedelta

from ..mailtext import clean_mail_body
from ..models import Document, SyncResult
from ..outlook.client import OutlookClient
from ..rules import is_excluded

RECONCILE_INTERVAL = timedelta(hours=24)


def backlog_hint(state: dict) -> bool:
    return bool(state.get("backlog"))


def _mail_document(m: dict) -> Document:
    body = clean_mail_body(m["body"])
    text = (
        f"보낸 사람: {m['sender_name']} <{m['sender_email']}>\n"
        f"받은 날짜: {m['received_at'].isoformat()}\n"
        f"제목: {m['subject']}\n\n{body}"
    )
    return Document(
        source_type="outlook_mail", source_id=m["entry_id"], title=m["subject"],
        text=text, url_or_path=f"outlook:{m['entry_id']}",
        updated_at=m["received_at"],
        extra={"sender": m["sender_email"], "folder": m["folder"]},
    )


def sync_outlook_mail(
    client: OutlookClient, folders: list[str], since_days: int, excludes: list[str],
    state: dict, batch_size: int = 200, now: datetime | None = None,
) -> SyncResult:
    if not client.is_available():
        raise RuntimeError("Outlook을 사용할 수 없습니다 — Windows에서 Outlook 실행 후 다시 시도하세요")
    now = now or datetime.now()
    floor = now - timedelta(days=since_days)
    cursor: dict = dict(state.get("cursor", {}))
    known: dict = {f: list(ids) for f, ids in state.get("known_ids", {}).items()}
    documents: list[Document] = []
    hit_batch_limit = False

    for folder in folders:
        since = max(
            datetime.fromisoformat(cursor[folder]) if folder in cursor else floor, floor
        )
        mails = client.list_mail(folder, since=since, limit=batch_size)
        if len(mails) >= batch_size:
            hit_batch_limit = True  # 콜드스타트 진행 중 — 다음 라운드에 이어서 (스펙 §7.4 P0)
            # 동시각 경계 보호: 커서가 exclusive(>)이므로, 가득 찬 배치의 꼬리 동시각
            # 그룹은 다음 라운드에 통째로 처리한다 (트림). 전부 동시각이면 트림 불가 —
            # batch_size 이상이 같은 초에 수신된 병리적 경우만 유실 가능(문서화된 한계).
            tail_ts = mails[-1]["received_at"]
            trimmed = [m for m in mails if m["received_at"] != tail_ts]
            if trimmed:
                mails = trimmed
        folder_known = set(known.get(folder, []))
        for m in mails:
            cursor[folder] = m["received_at"].isoformat()
            if is_excluded(None, m["sender_email"], folder, excludes):
                continue
            documents.append(_mail_document(m))
            folder_known.add(m["entry_id"])
        known[folder] = sorted(folder_known)

    # 삭제 대조: 정상 상태(배치 미포화) + 24시간 경과 시에만 (수만 통 ID 조회 비용 절약)
    deleted: list[str] = []
    last_reconcile = state.get("last_reconcile")
    due = last_reconcile is None or (now - datetime.fromisoformat(last_reconcile)) >= RECONCILE_INTERVAL
    if not hit_batch_limit and due:
        for folder in folders:
            existing = client.list_mail_ids(folder, since=floor)
            gone = [i for i in known.get(folder, []) if i not in existing]
            deleted.extend(gone)
            known[folder] = [i for i in known.get(folder, []) if i in existing]
        state_reconcile = now.isoformat()
    else:
        state_reconcile = last_reconcile

    return SyncResult(
        documents=documents, deleted_ids=deleted,
        state={"cursor": cursor, "known_ids": known,
               "last_reconcile": state_reconcile, "backlog": hit_batch_limit},
    )
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_outlook_mail.py -v` → PASS (5건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/connectors/outlook_mail.py tests/test_outlook_mail.py
git commit -m "feat: outlook_mail 커넥터 — 커서·배치 콜드스타트/절단/제외/삭제 대조"
```

---

### Task 6: outlook_cal 커넥터 — 기간 창, 회차 단위 인덱싱

**Files:**
- Create: `src/llmsearch/connectors/outlook_cal.py`
- Test: `tests/test_outlook_cal.py`

**Interfaces:**
- Consumes: `OutlookClient.list_appointments`, `models.Document/SyncResult`
- Produces:
  - `sync_outlook_cal(client: OutlookClient, past_days: int, future_days: int, state: dict, now: datetime | None = None) -> SyncResult`
  - `state`: `{"fingerprints": {occurrence_id: sha1(text)}}`; occurrence_id = `f"{entry_id}@{start.isoformat()}"` (반복 회차 구분)
  - **변경 감지**: 창 전체를 매번 조회하되, 지문(sha1 of text)이 바뀐 회차만 `documents`로 방출 — 매 30분 전량 재임베딩(API 비용) 방지. 창 밖으로 벗어났거나 취소된 회차는 `deleted_ids`로 보고(창 이동 자연 정리). 본문 텍스트에 날짜·시간 명시 (스펙 §7.5)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_outlook_cal.py`

```python
from datetime import datetime, timedelta

from llmsearch.connectors.outlook_cal import sync_outlook_cal
from llmsearch.outlook.client import FakeOutlookClient

NOW = datetime(2026, 8, 17, 9, 0)


def appt(eid, start, subject="팀 미팅"):
    return {"entry_id": eid, "subject": subject, "body": "안건", "location": "회의실A",
            "start": start, "end": start + timedelta(hours=1), "attendees": "나; 김철수"}


def test_indexes_occurrences_with_dates_in_text():
    c = FakeOutlookClient(appointments=[appt("e1", NOW + timedelta(days=3))])
    r = sync_outlook_cal(c, past_days=90, future_days=180, state={}, now=NOW)
    d = r.documents[0]
    assert d.source_type == "outlook_cal"
    assert d.source_id == f"e1@{(NOW + timedelta(days=3)).isoformat()}"
    assert "2026-08-20" in d.text and "회의실A" in d.text and "김철수" in d.text


def test_recurring_occurrences_distinct_ids():
    starts = [NOW + timedelta(days=7 * i) for i in range(3)]
    c = FakeOutlookClient(appointments=[appt("weekly", s) for s in starts])
    r = sync_outlook_cal(c, 90, 180, {}, now=NOW)
    assert len({d.source_id for d in r.documents}) == 3


def test_window_shift_deletes_stale():
    c = FakeOutlookClient(appointments=[appt("e1", NOW + timedelta(days=1))])
    r1 = sync_outlook_cal(c, 90, 180, {}, now=NOW)
    # 일정이 취소됨(목록에서 사라짐)
    c.appointments = []
    r2 = sync_outlook_cal(c, 90, 180, r1.state, now=NOW)
    assert r2.deleted_ids == [r1.documents[0].source_id]


def test_unchanged_appointment_not_reemitted():
    c = FakeOutlookClient(appointments=[appt("e1", NOW + timedelta(days=1))])
    r1 = sync_outlook_cal(c, 90, 180, {}, now=NOW)
    assert len(r1.documents) == 1
    r2 = sync_outlook_cal(c, 90, 180, r1.state, now=NOW)
    # 지문 미변경 → 재방출 없음 (매 동기화 전량 재임베딩 비용 방지)
    assert r2.documents == [] and r2.deleted_ids == []


def test_changed_appointment_reemitted():
    a = appt("e1", NOW + timedelta(days=1))
    c = FakeOutlookClient(appointments=[a])
    r1 = sync_outlook_cal(c, 90, 180, {}, now=NOW)
    a["location"] = "회의실B"  # 내용 변경 → 지문 변경
    r2 = sync_outlook_cal(c, 90, 180, r1.state, now=NOW)
    assert len(r2.documents) == 1 and "회의실B" in r2.documents[0].text
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_outlook_cal.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/connectors/outlook_cal.py`

```python
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from ..models import Document, SyncResult
from ..outlook.client import OutlookClient


def _occurrence_id(a: dict) -> str:
    return f"{a['entry_id']}@{a['start'].isoformat()}"


def _appt_document(a: dict) -> Document:
    text = (
        f"일정: {a['subject']}\n"
        f"날짜: {a['start'].date().isoformat()} ({a['start'].strftime('%H:%M')}~{a['end'].strftime('%H:%M')})\n"
        f"장소: {a['location']}\n참석자: {a['attendees']}\n\n{a['body']}"
    )
    return Document(
        source_type="outlook_cal", source_id=_occurrence_id(a), title=a["subject"],
        text=text, url_or_path=f"outlook:{a['entry_id']}",
        updated_at=a["start"], extra={"location": a["location"]},
    )


def sync_outlook_cal(
    client: OutlookClient, past_days: int, future_days: int, state: dict,
    now: datetime | None = None,
) -> SyncResult:
    if not client.is_available():
        raise RuntimeError("Outlook을 사용할 수 없습니다 — Windows에서 Outlook 실행 후 다시 시도하세요")
    now = now or datetime.now()
    window_start = now - timedelta(days=past_days)
    window_end = now + timedelta(days=future_days)
    appts = client.list_appointments(window_start, window_end)  # 반복 전개는 클라이언트 책임 (기간 한정, 스펙 §7.5)
    prev_fp: dict = state.get("fingerprints", {})
    current_fp: dict[str, str] = {}
    documents = []
    for a in appts:
        doc = _appt_document(a)
        fp = hashlib.sha1(doc.text.encode("utf-8")).hexdigest()
        current_fp[doc.source_id] = fp
        if prev_fp.get(doc.source_id) != fp:  # 신규·변경분만 방출 — 전량 재임베딩 방지
            documents.append(doc)
    deleted = [i for i in prev_fp if i not in current_fp]
    return SyncResult(documents=documents, deleted_ids=deleted, state={"fingerprints": current_fp})
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_outlook_cal.py -v` → PASS (5건), 전체 스위트 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/connectors/outlook_cal.py tests/test_outlook_cal.py
git commit -m "feat: outlook_cal 커넥터 — 기간 창/회차 ID/창 이동 정리"
```

---

### Task 7: ComOutlookClient + ThreadedOutlookClient + 수동 점검 스크립트

**Files:**
- Create: `src/llmsearch/outlook/com_client.py`, `scripts/check_outlook.py`
- Modify: `pyproject.toml` (optional-dependencies에 `win = ["pywin32>=306"]` 추가)
- Test: `tests/test_com_client.py` (import 가능성 + ThreadedOutlookClient 위임만 — 실 COM은 수동 스크립트)

**Interfaces:**
- Consumes: `ComWorker`(Task 4), `OutlookClient` 계약(Task 3)
- Produces:
  - `ComOutlookClient` — 실 COM 구현. **반드시 ComWorker 스레드에서 생성·호출**되어야 함 (직접 사용 금지, ThreadedOutlookClient로만)
  - `ThreadedOutlookClient(worker: ComWorker)` — OutlookClient 구현: 모든 호출을 `worker.submit`으로 위임, 실 클라이언트는 워커 스레드에서 최초 1회 지연 생성
- WSL 테스트 범위: 모듈 import(최상위 win32com import 없음 검증) + ThreadedOutlookClient가 워커 스레드에서 팩토리·메서드를 실행하는지(주입 가능한 `client_factory`로 검증). COM 자체는 `scripts/check_outlook.py` 수동 실행

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_com_client.py`

```python
import threading
from datetime import datetime

from llmsearch.outlook.com_worker import ComWorker


def test_module_importable_without_pywin32():
    import llmsearch.outlook.com_client  # noqa: F401 — 최상위 win32com import가 없어야 함


def test_threaded_client_delegates_on_worker_thread():
    from llmsearch.outlook.com_client import ThreadedOutlookClient

    calls = []

    class Probe:
        def is_available(self):
            calls.append(("avail", threading.current_thread().name))
            return True

        def list_mail(self, folder, since, until=None, limit=None):
            calls.append(("mail", threading.current_thread().name))
            return []

    w = ComWorker()
    try:
        c = ThreadedOutlookClient(w, client_factory=Probe)
        assert c.is_available() is True
        assert c.list_mail("inbox", since=datetime(2026, 1, 1)) == []
        assert all(thread == "com-worker" for _, thread in calls)
        # 팩토리는 1회만 (워커 스레드에서 지연 생성)
        assert c._client is not None
    finally:
        w.shutdown()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_com_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/outlook/com_client.py`

```python
"""실제 Outlook COM 접근 (Windows 전용).

ComOutlookClient는 반드시 ComWorker 스레드에서 생성·사용해야 한다(아파트먼트 친화성).
외부에서는 ThreadedOutlookClient만 사용할 것.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from .com_worker import ComWorker

_FOLDER_IDS = {"inbox": 6, "sent": 5}  # OlDefaultFolders 상수
_CALENDAR_ID = 9
_RESTRICT_FMT = "%m/%d/%Y %I:%M %p"  # Outlook Restrict가 요구하는 미국식 포맷


class ComOutlookClient:
    def __init__(self):
        import win32com.client  # 지연 import — Windows + pywin32 전용

        self._app = win32com.client.Dispatch("Outlook.Application")
        self._ns = self._app.GetNamespace("MAPI")

    def _folder(self, name: str):
        if name in _FOLDER_IDS:
            return self._ns.GetDefaultFolder(_FOLDER_IDS[name])
        # 커스텀 폴더: 기본 스토어의 받은편지함 형제/하위에서 이름으로 탐색
        inbox = self._ns.GetDefaultFolder(6)
        for candidate in (inbox.Folders, inbox.Parent.Folders):
            for f in candidate:
                if f.Name == name:
                    return f
        raise KeyError(f"Outlook 폴더를 찾을 수 없습니다: {name}")

    def is_available(self) -> bool:
        try:
            _ = self._ns.GetDefaultFolder(6).Name
            return True
        except Exception:
            return False

    def _mail_dict(self, item, folder: str) -> dict:
        received = item.ReceivedTime
        return {
            "entry_id": item.EntryID,
            "subject": item.Subject or "(제목 없음)",
            "body": item.Body or "",
            "sender_name": getattr(item, "SenderName", "") or "",
            "sender_email": getattr(item, "SenderEmailAddress", "") or "",
            "received_at": datetime(received.year, received.month, received.day,
                                    received.hour, received.minute, received.second),
            "folder": folder,
        }

    def list_mail(self, folder: str, since: datetime, until: datetime | None = None,
                  limit: int | None = None) -> list[dict]:
        items = self._folder(folder).Items
        items.Sort("[ReceivedTime]", False)  # 오름차순
        query = f"[ReceivedTime] > '{since.strftime(_RESTRICT_FMT)}'"
        if until is not None:
            query += f" AND [ReceivedTime] <= '{until.strftime(_RESTRICT_FMT)}'"
        restricted = items.Restrict(query)
        out: list[dict] = []
        for item in restricted:
            if getattr(item, "Class", None) != 43:  # olMail만 (회의요청 등 제외)
                continue
            out.append(self._mail_dict(item, folder))
            if limit is not None and len(out) >= limit:
                break
        return out

    def list_mail_ids(self, folder: str, since: datetime) -> set[str]:
        items = self._folder(folder).Items
        items.Sort("[ReceivedTime]", False)
        restricted = items.Restrict(f"[ReceivedTime] > '{since.strftime(_RESTRICT_FMT)}'")
        return {item.EntryID for item in restricted if getattr(item, "Class", None) == 43}

    def open_item(self, entry_id: str) -> None:
        self._ns.GetItemFromID(entry_id).Display()  # Outlook 창으로 표시 (스펙 §7.4)

    def list_appointments(self, window_start: datetime, window_end: datetime) -> list[dict]:
        items = self._ns.GetDefaultFolder(_CALENDAR_ID).Items
        items.Sort("[Start]")            # IncludeRecurrences 전에 Sort 필수 (Outlook 규약)
        items.IncludeRecurrences = True  # 반복 일정 전개 — Restrict로 기간 한정 (스펙 §7.5)
        query = (f"[Start] >= '{window_start.strftime(_RESTRICT_FMT)}'"
                 f" AND [Start] <= '{window_end.strftime(_RESTRICT_FMT)}'")
        out: list[dict] = []
        for item in items.Restrict(query):
            start, end = item.Start, item.End
            out.append({
                "entry_id": item.EntryID,
                "subject": item.Subject or "(제목 없음)",
                "body": item.Body or "",
                "location": item.Location or "",
                "start": datetime(start.year, start.month, start.day, start.hour, start.minute),
                "end": datetime(end.year, end.month, end.day, end.hour, end.minute),
                "attendees": (getattr(item, "RequiredAttendees", "") or ""),
            })
        return out


class ThreadedOutlookClient:
    """OutlookClient 구현 — 모든 호출을 ComWorker 스레드로 위임 (스펙 §5 P0)."""

    def __init__(self, worker: ComWorker, client_factory: Callable = ComOutlookClient):
        self._worker = worker
        self._factory = client_factory
        self._client = None

    def _call(self, method: str, *args, **kwargs):
        def run():
            if self._client is None:
                self._client = self._factory()  # 워커 스레드에서 생성 (아파트먼트 친화성)
            return getattr(self._client, method)(*args, **kwargs)

        return self._worker.submit(run)

    def is_available(self) -> bool:
        try:
            return self._call("is_available")
        except Exception:
            return False

    def list_mail(self, folder, since, until=None, limit=None):
        return self._call("list_mail", folder, since, until=until, limit=limit)

    def list_mail_ids(self, folder, since):
        return self._call("list_mail_ids", folder, since)

    def list_appointments(self, window_start, window_end):
        return self._call("list_appointments", window_start, window_end)

    def open_item(self, entry_id: str) -> None:
        self._call("open_item", entry_id)
```

`scripts/check_outlook.py` (Windows에서 수동 실행 — 자동 테스트 아님):

```python
"""Windows에서 Outlook COM 연동 수동 점검 (스펙 §12: COM은 자동 테스트 불가).

사용: (Outlook 실행 상태에서) python scripts/check_outlook.py
"""
from datetime import datetime, timedelta

from llmsearch.outlook.com_client import ThreadedOutlookClient
from llmsearch.outlook.com_worker import ComWorker

worker = ComWorker()
try:
    client = ThreadedOutlookClient(worker)
    print("가용성:", client.is_available())
    since = datetime.now() - timedelta(days=7)
    mails = client.list_mail("inbox", since=since, limit=5)
    print(f"최근 7일 받은편지함 {len(mails)}통 (최대 5):")
    for m in mails:
        print(" -", m["received_at"], m["sender_email"], "|", m["subject"][:40])
    appts = client.list_appointments(datetime.now() - timedelta(days=7),
                                     datetime.now() + timedelta(days=14))
    print(f"±기간 일정 {len(appts)}건:")
    for a in appts[:5]:
        print(" -", a["start"], "|", a["subject"][:40])
    print("OK")
finally:
    worker.shutdown()
```

`pyproject.toml`의 `[project.optional-dependencies]`에 한 줄 추가:

```toml
win = ["pywin32>=306"]
```

- [ ] **Step 4: 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/test_com_client.py -v` → PASS (2건), 전체 스위트 PASS (pywin32 미설치 환경에서)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/outlook/com_client.py scripts/check_outlook.py pyproject.toml tests/test_com_client.py
git commit -m "feat: ComOutlookClient/ThreadedOutlookClient + Windows 수동 점검 스크립트"
```

---

### Task 8: config 확장 + 웹앱 통합 + README

**Files:**
- Modify: `src/llmsearch/config.py`, `src/llmsearch/web/app.py`, `config.example.yaml`, `README.md`
- Test: `tests/test_web_outlook.py`, `tests/test_config.py` (추가)

**Interfaces:**
- Consumes: Task 5·6 커넥터, Task 7 `ThreadedOutlookClient`/`ComWorker`, 기존 `run_sync`/`sync_lock`/`SOURCES` 패턴
- Produces:
  - `Config`에 추가 (기본값 포함): `mail_folders: list[str] = ["inbox", "sent"]`, `mail_since_days: int = 365`, `mail_batch_size: int = 200`, `cal_past_days: int = 90`, `cal_future_days: int = 180` — yaml 키는 `outlook: {mail_folders, mail_since_days, mail_batch_size, cal_past_days, cal_future_days}`
  - `create_app(..., outlook_client=None)` — 주입 없으면 첫 Outlook 동기화 때 `ComWorker`+`ThreadedOutlookClient` 지연 생성. `SOURCES = ("notes", "local_docs", "outlook_mail", "outlook_cal")`
  - `/api/sources`의 outlook_mail 항목에 `"backlog": bool` 포함 (콜드스타트 진행 중 표시, 스펙 §7.4 GUI 진행률)
  - `POST /api/open` `{"url_or_path": str}` — `outlook:<entry_id>`면 `client.open_item(entry_id)`, 로컬 경로면 Windows에서 `os.startfile` (비Windows는 `{"ok": false, "error": ...}` 반환). 출처 카드에 "열기" 버튼 (스펙 §7.4/§8 원본 열기)
  - Outlook 불가(사전 조건 실패) 시 run_sync가 로그에 명확한 안내를 남기고 다른 소스는 계속 동작 (스펙 §5 격리)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_config.py`에 추가:

```python
def test_outlook_config_defaults_and_load(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "data_dir: /d\noutlook:\n  mail_folders: [\"inbox\"]\n  mail_since_days: 30\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.mail_folders == ["inbox"]
    assert cfg.mail_since_days == 30
    assert cfg.mail_batch_size == 200      # 기본값
    assert cfg.cal_past_days == 90 and cfg.cal_future_days == 180
```

`tests/test_web_outlook.py` (새 파일):

```python
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from llmsearch.config import Config
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.llm import FakeAnswerer
from llmsearch.outlook.client import FakeOutlookClient
from llmsearch.summarize import FakeSummarizer
from llmsearch.web.app import create_app

NOW = datetime.now()


def make_client(tmp_path: Path, outlook=None) -> TestClient:
    cfg = Config(data_dir=tmp_path / "data")
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), outlook_client=outlook, enable_scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1")


def fake_outlook():
    return FakeOutlookClient(mails={"inbox": [{
        "entry_id": "m1", "subject": "프로젝트A 결정사항", "body": "회의 결과 공유",
        "sender_name": "김철수", "sender_email": "kim@corp.com",
        "received_at": NOW - timedelta(days=1), "folder": "inbox",
    }]}, appointments=[{
        "entry_id": "e1", "subject": "주간 회의", "body": "", "location": "회의실",
        "start": NOW + timedelta(days=2), "end": NOW + timedelta(days=2, hours=1),
        "attendees": "나",
    }])


def test_outlook_sources_listed(tmp_path: Path):
    client = make_client(tmp_path, outlook=fake_outlook())
    sources = {s["source"] for s in client.get("/api/sources").json()}
    assert {"outlook_mail", "outlook_cal"} <= sources


def test_mail_sync_and_search(tmp_path: Path):
    client = make_client(tmp_path, outlook=fake_outlook())
    r = client.post("/api/sync/outlook_mail")
    assert r.status_code == 200 and r.json()["indexed"] == 1
    mail_status = next(s for s in client.get("/api/sources").json() if s["source"] == "outlook_mail")
    assert mail_status["doc_count"] == 1
    assert mail_status["backlog"] is False


def test_cal_sync(tmp_path: Path):
    client = make_client(tmp_path, outlook=fake_outlook())
    assert client.post("/api/sync/outlook_cal").json()["indexed"] == 1


def test_outlook_unavailable_isolated(tmp_path: Path):
    client = make_client(tmp_path, outlook=FakeOutlookClient(available=False))
    r = client.post("/api/sync/outlook_mail")
    assert r.status_code == 200
    assert r.json()["ok"] is False and "Outlook" in r.json()["error"]
    # 다른 소스는 정상 동작
    assert client.post("/api/sync/notes").status_code == 200


def test_open_outlook_item(tmp_path: Path):
    fake = fake_outlook()
    client = make_client(tmp_path, outlook=fake)
    r = client.post("/api/open", json={"url_or_path": "outlook:m1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert fake.opened == ["m1"]


def test_open_local_path_non_windows(tmp_path: Path):
    """비Windows(WSL 테스트 환경)에서는 파일 열기가 안내 오류로 응답해야 한다."""
    import sys
    client = make_client(tmp_path)
    f = tmp_path / "a.md"; f.write_text("x", encoding="utf-8")
    r = client.post("/api/open", json={"url_or_path": str(f)})
    assert r.status_code == 200
    if sys.platform != "win32":
        assert r.json()["ok"] is False
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `./.venv/bin/python -m pytest tests/test_web_outlook.py tests/test_config.py -v`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'outlook_client'` 등

- [ ] **Step 3: 구현**

`config.py` — `Config`에 필드 추가:

```python
    mail_folders: list[str] = field(default_factory=lambda: ["inbox", "sent"])
    mail_since_days: int = 365
    mail_batch_size: int = 200
    cal_past_days: int = 90
    cal_future_days: int = 180
```

`load_config`에 파싱 추가 — 함수 본문 앞부분에 `outlook = raw.get("outlook", {})` 한 줄을 추가하고, `Config(...)` 생성자 인자 목록에 다음 5줄을 추가한다 (기존 인자는 그대로):

```python
    outlook = raw.get("outlook", {})
```

```python
        mail_folders=list(outlook.get("mail_folders", ["inbox", "sent"])),
        mail_since_days=int(outlook.get("mail_since_days", 365)),
        mail_batch_size=int(outlook.get("mail_batch_size", 200)),
        cal_past_days=int(outlook.get("cal_past_days", 90)),
        cal_future_days=int(outlook.get("cal_future_days", 180)),
```

`web/app.py` 수정 요지 (기존 구조 유지 — M1 코드가 정본):

```python
from ..connectors.outlook_cal import sync_outlook_cal
from ..connectors.outlook_mail import backlog_hint, sync_outlook_mail

SOURCES = ("notes", "local_docs", "outlook_mail", "outlook_cal")


def _get_outlook_client(state):
    """실 클라이언트 지연 생성 — 테스트는 create_app 주입으로 이 경로를 타지 않는다."""
    if state.get("outlook_client") is None:
        from ..outlook.com_client import ThreadedOutlookClient
        from ..outlook.com_worker import ComWorker

        worker = ComWorker()
        state["outlook_worker"] = worker
        state["outlook_client"] = ThreadedOutlookClient(worker)
    return state["outlook_client"]
```

`run_sync`의 소스 분기에 추가 (기존 try/except + sync_lock 안):

```python
        elif source == "outlook_mail":
            client = _get_outlook_client(state)
            result = sync_outlook_mail(
                client, cfg.mail_folders, cfg.mail_since_days, cfg.exclude,
                prev, batch_size=cfg.mail_batch_size,
            )
        elif source == "outlook_cal":
            client = _get_outlook_client(state)
            result = sync_outlook_cal(client, cfg.cal_past_days, cfg.cal_future_days, prev)
```

`create_app` 시그니처에 `outlook_client=None` 추가, `state["outlook_client"] = outlook_client`.

`/api/open` 엔드포인트 추가 (라우트들 옆에):

```python
    @app.post("/api/open")
    def open_item(payload: dict):
        target = str(payload.get("url_or_path", ""))
        try:
            if target.startswith("outlook:"):
                _get_outlook_client(state).open_item(target.removeprefix("outlook:"))
                return {"ok": True}
            import os
            if hasattr(os, "startfile"):  # Windows 전용
                os.startfile(target)  # noqa: S606 — 로컬 개인 도구, 사용자 소유 경로
                return {"ok": True}
            return {"ok": False, "error": "파일 열기는 Windows에서만 지원됩니다"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
```

`web/static/index.html`의 출처 카드 템플릿에 열기 버튼 추가 — 기존 `<code>${esc(h.url_or_path)}</code>` 옆에:

```javascript
`<button onclick="openItem(this.dataset.p)" data-p="${esc(h.url_or_path)}">열기</button>`
```

와 스크립트 함수:

```javascript
async function openItem(p) {
  const r = await (await fetch('/api/open', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url_or_path: p})})).json();
  if (!r.ok) alert(r.error);
}
```

`/api/sources` 응답에 outlook_mail 전용 필드 추가:

```python
            entry = {"source": source, "doc_count": row[0],
                     "last_sync": last["at"] if last else None,
                     "last_error": last["error"] if last else None}
            if source == "outlook_mail":
                entry["backlog"] = backlog_hint(indexer.get_sync_state(read_conn, source))
            out.append(entry)
```

`config.example.yaml`에 추가:

```yaml
outlook:
  mail_folders: ["inbox", "sent"]   # 커스텀 폴더는 Outlook 폴더명 그대로
  mail_since_days: 365
  mail_batch_size: 200              # 콜드스타트 1회당 처리량 (재개 가능)
  cal_past_days: 90
  cal_future_days: 180
```

`README.md`에 M2 절 추가:

```markdown
## Outlook 연동 (M2, Windows 전용)
1. Windows Python에 `pip install -e ".[vec,win]"` (pywin32 포함)
2. Outlook 데스크톱 앱을 실행해 둔 상태에서 `python scripts/check_outlook.py`로 연동 점검
3. 앱 실행 후 소스 탭에서 outlook_mail / outlook_cal 동기화 — 초기 메일 인덱싱은
   배치(기본 200통)로 진행되며 중단해도 다음 동기화가 이어서 처리한다 (backlog 표시)
- WSL/테스트 환경에서는 Outlook 소스 동기화 시 안내 오류가 로그에 남고 다른 소스는 정상 동작
```

- [ ] **Step 4: 전체 테스트 통과 확인** — Run: `./.venv/bin/python -m pytest tests/ -q` → 전부 PASS (기존 회귀 포함)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/config.py src/llmsearch/web/app.py config.example.yaml README.md tests/test_web_outlook.py tests/test_config.py
git commit -m "feat: Outlook 소스 웹 통합 — 설정/지연 클라이언트/backlog 표시/격리"
```

---

## M2 완료 기준 (수동 검증 체크리스트 — Windows)

1. `pip install -e ".[vec,win]"` 후 `python scripts/check_outlook.py` — 최근 메일 5통·일정 목록 출력 확인
2. 앱 실행 → outlook_mail 수동 동기화 반복 → backlog가 True→False로 전환되며 커서가 전진하는지 (콜드스타트 재개)
3. 채팅에서 "지난주 김OO가 보낸 메일" 류 질문 → 출처에 메일 카드(`outlook:` 경로) 표시
4. "다음 주 일정 뭐 있지?" → 일정 문서가 date 필터 경로로 검색되는지
5. Outlook 종료 상태에서 동기화 → 로그에 안내 오류, notes/local_docs는 정상 동작
6. 출처 카드의 "열기" 버튼 — 메일은 Outlook 창, 로컬 문서는 연결 프로그램으로 열리는지
```
