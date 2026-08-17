# llmsearch M5 — 비용 통제(P2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스펙 §10 P2 구현 — 누적 API 사용량 카운터(로그 출력) + 일일 호출 상한, 상한 도달 시 요약·인덱싱만 일시정지하고 검색·답변은 유지.

**Architecture:** `usage.py`에 `UsageTracker`(usage.json 영속, 일자별 카운터, 상한 판정, 내부 threading.Lock — chat 스레드와 동기화 스레드가 동시에 기록한다)와 카운팅 래퍼(`CountingEmbedder`/`CountingSummarizer`)를 둔다. 래퍼는 기록만 하고 차단하지 않는다 — 차단 결정은 웹 계층 한 곳(`run_sync` 진입 게이트)에서만 내려, 검색 경로(쿼리 임베딩·Claude 답변)는 상한과 무관하게 계속 동작한다. **의도된 설계 결정: 검색·답변 호출도 같은 일일 카운터에 합산된다** (스펙 §10 "일일 API 호출 상한"은 전체 호출 기준) — 인덱싱 호출만 세도록 임의 변경하지 말 것. GUI 표시는 스펙대로 추후(v1은 로그·동기화 로그 엔트리로 노출).

**Tech Stack:** Python 3.12, 표준 라이브러리만 (신규 의존성 없음)

**Spec:** `docs/superpowers/specs/2026-08-17-llmsearch-design.md` §10 (비용·보안 통제)

## Global Constraints

- 상한은 **요약·인덱싱 경로에만** 적용 — 검색(쿼리 임베딩)·채팅 답변은 상한 도달 후에도 정상 동작 (스펙 §10)
- v1은 로그 출력 — GUI 표시는 범위 밖 (스펙 §10 "GUI 표시는 추후")
- 카운팅 래퍼는 주입된 Fake 구현과도 동일하게 동작해야 하며, 기존 테스트 248개는 무변경 통과
- Python 들여쓰기 4칸 (신규 파일 — 전역 CLAUDE.md의 탭 규칙은 GDScript 전용)
- 전체 스위트 `./.venv/bin/pytest` — 시작 기준 248개, 태스크마다 green 유지
- 커밋 메시지는 기존 스타일(한국어, `feat:`/`test:` 접두사)

---

### Task 1: UsageTracker — 일자별 카운터·영속·상한 판정

**Files:**
- Create: `src/llmsearch/usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Consumes: 없음 (표준 라이브러리만)
- Produces: `UsageTracker(path: Path, daily_limit: int = 0)` — `record(kind: str, count: int = 1)`, `today_total() -> int`, `indexing_allowed() -> bool` (daily_limit 0 = 무제한). Task 2의 래퍼와 Task 3의 웹 게이트가 이 시그니처를 사용한다. 날짜는 모듈 전역 `date`를 통해 얻으므로 테스트는 `llmsearch.usage.date`를 monkeypatch할 수 있다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_usage.py` 신규:

```python
import json
from datetime import date
from pathlib import Path

from llmsearch import usage
from llmsearch.usage import UsageTracker


def test_record_accumulates_and_persists(tmp_path: Path):
    t = UsageTracker(tmp_path / "usage.json")
    t.record("embed")
    t.record("embed", 2)
    t.record("summary")
    assert t.today_total() == 4
    saved = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    today = date.today().isoformat()
    assert saved[today] == {"embed": 3, "summary": 1}


def test_reload_from_disk(tmp_path: Path):
    t1 = UsageTracker(tmp_path / "usage.json")
    t1.record("embed", 5)
    t2 = UsageTracker(tmp_path / "usage.json")
    assert t2.today_total() == 5


def test_limit_zero_is_unlimited(tmp_path: Path):
    t = UsageTracker(tmp_path / "usage.json", daily_limit=0)
    t.record("embed", 10_000)
    assert t.indexing_allowed() is True


def test_limit_reached_blocks_indexing(tmp_path: Path):
    t = UsageTracker(tmp_path / "usage.json", daily_limit=3)
    t.record("embed", 2)
    assert t.indexing_allowed() is True  # 2 < 3
    t.record("summary")
    assert t.indexing_allowed() is False  # 3 >= 3


def test_day_rollover_resets_today(tmp_path: Path, monkeypatch):
    """어제 카운트는 오늘 합계·상한 판정에 영향을 주지 않는다."""
    t = UsageTracker(tmp_path / "usage.json", daily_limit=3)
    t.record("embed", 3)
    assert t.indexing_allowed() is False

    class Tomorrow:
        @staticmethod
        def today():
            return date.fromordinal(date.today().toordinal() + 1)

    monkeypatch.setattr(usage, "date", Tomorrow)
    assert t.today_total() == 0
    assert t.indexing_allowed() is True


def test_old_days_pruned(tmp_path: Path):
    path = tmp_path / "usage.json"
    stale = {f"2020-01-{d:02d}": {"embed": 1} for d in range(1, 32)}
    path.write_text(json.dumps(stale), encoding="utf-8")
    t = UsageTracker(path)
    t.record("embed")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) <= usage._KEEP_DAYS


def test_corrupt_file_starts_fresh(tmp_path: Path):
    path = tmp_path / "usage.json"
    path.write_text("{broken", encoding="utf-8")
    t = UsageTracker(path)
    assert t.today_total() == 0
    t.record("embed")  # 손상 파일 위에도 정상 기록
    assert t.today_total() == 1
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_usage.py -v`
Expected: FAIL — `ModuleNotFoundError: llmsearch.usage`

- [ ] **Step 3: 구현**

`src/llmsearch/usage.py` 신규:

```python
"""API 사용량 카운터 + 일일 호출 상한 (스펙 §10 P2 — v1은 로그 출력, GUI 표시는 추후).

`usage.json`에 {"YYYY-MM-DD": {"embed": n, "summary": n, "vision": n, "answer": n}}
형태로 영속화한다. 상한(daily_limit)은 요약·인덱싱 경로에만 적용된다 — 적용 지점은
웹 계층의 run_sync 진입 게이트이고, 트래커와 카운팅 래퍼 자신은 절대 차단하지 않는다.
그래야 검색(쿼리 임베딩)·채팅 답변이 상한 도달 후에도 계속 동작한다 (스펙 §10:
"상한 도달 시 요약·인덱싱만 일시정지, 검색은 유지").
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_KEEP_DAYS = 30  # usage.json에 보관하는 일자 수 — 그 이전은 기록 시점에 정리


class UsageTracker:
    def __init__(self, path: Path, daily_limit: int = 0):
        self.path = path
        self.daily_limit = daily_limit  # 0 이하 = 무제한
        # chat(FastAPI 스레드풀)과 run_sync(스케줄러/수동 동기화 스레드)가 동시에 기록한다 —
        # sync_lock은 run_sync 쪽만 잡으므로 트래커가 자체 락으로 dict 변이·파일 쓰기를 직렬화.
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, int]] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("usage.json 파싱 실패 — 카운터를 새로 시작합니다: %s", path)
                self._data = {}

    def _today(self) -> str:
        return date.today().isoformat()

    def record(self, kind: str, count: int = 1) -> None:
        with self._lock:
            day_data = self._data.setdefault(self._today(), {})
            day_data[kind] = day_data.get(kind, 0) + count
            for old in sorted(self._data)[:-_KEEP_DAYS]:
                del self._data[old]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            total = sum(self._data.get(self._today(), {}).values())
        logger.info(
            "API 사용량 +%d(%s) — 오늘 누적 %d건, 일일 상한 %s",
            count, kind, total,
            self.daily_limit if self.daily_limit > 0 else "없음",
        )

    def today_total(self) -> int:
        with self._lock:
            return sum(self._data.get(self._today(), {}).values())

    def indexing_allowed(self) -> bool:
        return self.daily_limit <= 0 or self.today_total() < self.daily_limit
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_usage.py -v` → PASS, `./.venv/bin/pytest -q` 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/usage.py tests/test_usage.py
git commit -m "feat: UsageTracker — 일자별 API 사용량 카운터·영속·일일 상한 판정 (스펙 §10 P2)"
```

---

### Task 2: 카운팅 래퍼 — CountingEmbedder / CountingSummarizer

**Files:**
- Modify: `src/llmsearch/usage.py`
- Test: `tests/test_usage.py` (추가)

**Interfaces:**
- Consumes: Task 1의 `UsageTracker`; 기존 `EmbeddingProvider.embed(texts) -> list[list[float]]`; 기존 `Summarizer` 3메서드(summarize_and_classify, describe_filename, describe_images)
- Produces: `CountingEmbedder(inner, tracker)` — embed 호출 1건당 `record("embed")` 후 위임; `CountingSummarizer(inner, tracker)` — summarize_and_classify/describe_filename은 `record("summary")`, describe_images는 `record("vision")` 후 위임. 어느 쪽도 차단하지 않는다. Task 3이 create_app에서 이 래퍼로 감싼다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_usage.py`에 추가:

```python
def test_counting_embedder_records_and_delegates(tmp_path: Path):
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.usage import CountingEmbedder

    t = UsageTracker(tmp_path / "usage.json")
    e = CountingEmbedder(FakeEmbeddings(), t)
    out = e.embed(["가", "나"])
    assert len(out) == 2 and len(out[0]) > 0  # 위임 결과 그대로
    e.embed(["다"])
    assert t.today_total() == 2  # 호출 단위 기록 (텍스트 수 아님 — 배치 1회 = API 1회)


def test_counting_embedder_records_even_at_limit(tmp_path: Path):
    """래퍼는 차단하지 않는다 — 상한 도달 후에도 검색 쿼리 임베딩은 동작해야 한다."""
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.usage import CountingEmbedder

    t = UsageTracker(tmp_path / "usage.json", daily_limit=1)
    e = CountingEmbedder(FakeEmbeddings(), t)
    e.embed(["가"])
    assert t.indexing_allowed() is False
    assert len(e.embed(["나"])) == 1  # 차단 없이 정상 위임


def test_counting_summarizer_records_by_kind(tmp_path: Path):
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.usage import CountingSummarizer

    t = UsageTracker(tmp_path / "usage.json")
    s = CountingSummarizer(FakeSummarizer(), t)
    r = s.summarize_and_classify(title="문서", text="프로젝트A 내용", projects=["프로젝트A"],
                                 areas=[], existing_resources=[], prior_category=None,
                                 glossary="", rules="")
    assert r.category == "Projects/프로젝트A"  # 위임 결과 그대로
    assert "파일명 기반" in s.describe_filename("보고서.pptx")
    assert "2장" in s.describe_images("덱.pptx", [b"a", b"b"])
    saved = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    today = date.today().isoformat()
    assert saved[today] == {"summary": 2, "vision": 1}
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_usage.py -v -k counting`
Expected: FAIL — `ImportError: CountingEmbedder`

- [ ] **Step 3: 구현**

`src/llmsearch/usage.py`에 추가 (UsageTracker 아래):

```python
class CountingEmbedder:
    """EmbeddingProvider 래퍼 — 배치 호출 1건당 record("embed"). 차단하지 않는다."""

    def __init__(self, inner, tracker: UsageTracker):
        self.inner = inner
        self.tracker = tracker

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.tracker.record("embed")
        return self.inner.embed(texts)


class CountingSummarizer:
    """Summarizer 래퍼 — 요약·파일명 설명은 "summary", 비전 설명은 "vision"으로 기록."""

    def __init__(self, inner, tracker: UsageTracker):
        self.inner = inner
        self.tracker = tracker

    def summarize_and_classify(self, *args, **kwargs):
        self.tracker.record("summary")
        return self.inner.summarize_and_classify(*args, **kwargs)

    def describe_filename(self, filename: str) -> str:
        self.tracker.record("summary")
        return self.inner.describe_filename(filename)

    def describe_images(self, title: str, images: list[bytes]) -> str:
        self.tracker.record("vision")
        return self.inner.describe_images(title, images)
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_usage.py -v` → PASS, `./.venv/bin/pytest -q` 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/usage.py tests/test_usage.py
git commit -m "feat: CountingEmbedder/CountingSummarizer — 종류별 사용량 기록 래퍼 (차단 없음)"
```

---

### Task 3: 웹 통합 — config 키, create_app 배선, run_sync 게이트, 문서

**Files:**
- Modify: `src/llmsearch/config.py`, `src/llmsearch/web/app.py`, `config.example.yaml`, `README.md`
- Test: `tests/test_config.py`, `tests/test_web.py` (추가)

**Interfaces:**
- Consumes: Task 1-2의 `UsageTracker`/`CountingEmbedder`/`CountingSummarizer`
- Produces: `Config.daily_api_call_limit: int = 0` (yaml `limits.daily_api_calls`); `state["usage"]` = UsageTracker; run_sync는 상한 도달 시 동기화를 건너뛰고 `entry["error"]`에 한국어 안내를 남긴다; `/api/chat`은 요청당 `record("answer")`.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_config.py`에 추가 (기존 load_config 테스트 관례에 맞춰):

```python
def test_daily_api_call_limit_loaded(tmp_path):
    from llmsearch.config import load_config

    p = tmp_path / "config.yaml"
    p.write_text("data_dir: /tmp/x\nlimits:\n  daily_api_calls: 500\n", encoding="utf-8")
    assert load_config(p).daily_api_call_limit == 500


def test_daily_api_call_limit_default_zero(tmp_path):
    from llmsearch.config import load_config

    p = tmp_path / "config.yaml"
    p.write_text("data_dir: /tmp/x\n", encoding="utf-8")
    assert load_config(p).daily_api_call_limit == 0
```

`tests/test_web.py`에 추가 (이 파일의 기존 create_app 관례 재사용):

```python
def test_sync_paused_at_daily_limit_but_chat_still_works(tmp_path):
    """스펙 §10: 상한 도달 시 요약·인덱싱만 일시정지, 검색·답변은 유지."""
    from fastapi.testclient import TestClient

    from llmsearch.config import Config
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app, run_sync

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# 메모\n프로젝트A 내용", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], daily_api_call_limit=1)
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    state = app.state.llmsearch

    entry1 = run_sync(state, "notes")  # 인덱싱 1회 → embed 1건 기록 → 상한(1) 도달
    assert entry1["ok"] is True and entry1["indexed"] == 1

    entry2 = run_sync(state, "notes")  # 이제 게이트에 걸림
    assert entry2["ok"] is False and entry2["indexed"] == 0
    assert "일일 API 호출 상한" in entry2["error"] and "검색" in entry2["error"]
    assert state["log"][0]["error"] == entry2["error"]  # 로그 탭에 노출

    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post("/api/chat", json={"question": "프로젝트A 뭐였지?", "history": []})
    assert r.status_code == 200
    assert "event: done" in r.text  # 상한 도달 후에도 채팅 스트림 정상 완료


def test_chat_records_answer_usage(tmp_path):
    from fastapi.testclient import TestClient

    from llmsearch.config import Config
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app

    app = create_app(Config(data_dir=tmp_path / "data"), embedder=FakeEmbeddings(),
                     summarizer=FakeSummarizer(), answerer=FakeAnswerer(),
                     enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.post("/api/chat", json={"question": "q", "history": []})
    tracker = app.state.llmsearch["usage"]
    today = tracker._data.get(tracker._today(), {})
    assert today.get("answer", 0) >= 1
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_config.py tests/test_web.py -v -k "limit or usage"`
Expected: FAIL — config 테스트는 `AttributeError: 'Config' object has no attribute 'daily_api_call_limit'` (load_config 경유), test_web 쪽은 `TypeError: unexpected keyword 'daily_api_call_limit'`(Config 직접 생성) 및 KeyError `usage`

- [ ] **Step 3: 구현**

`src/llmsearch/config.py`:

`Config` 필드에 추가 (`jira_base_url` 아래):

```python
    daily_api_call_limit: int = 0  # 0 = 무제한 (스펙 §10 P2)
```

`load_config`에서 `atlassian = raw.get("atlassian", {})` 아래에 추가:

```python
    limits = raw.get("limits", {})
```

`Config(...)` 인자 목록 끝에 추가:

```python
        daily_api_call_limit=int(limits.get("daily_api_calls", 0)),
```

`src/llmsearch/web/app.py`:

import에 추가:

```python
from ..usage import CountingEmbedder, CountingSummarizer, UsageTracker
```

`create_app`에서 embedder/summarizer 기본값 해석 직후(answerer 블록 뒤, `conn = db.open_db(...)` 앞)에 추가:

```python
    # 사용량 카운팅 래퍼 (스펙 §10 P2) — 주입된 Fake 포함 모든 경로를 기록.
    # 래퍼는 기록만 하고 차단하지 않는다 — 차단은 run_sync 진입 게이트 한 곳에서만.
    tracker = UsageTracker(config.data_dir / "usage.json", config.daily_api_call_limit)
    embedder = CountingEmbedder(embedder, tracker)
    summarizer = CountingSummarizer(summarizer, tracker)
```

`state` 딕셔너리에 항목 추가 (registry 항목 뒤):

```python
             "usage": tracker,
```

`run_sync`의 `with state["sync_lock"]:` 진입 직후, `try:` 앞에 게이트 추가:

```python
    with state["sync_lock"]:  # 단일 sqlite3.Connection 공유 쓰기 직렬화 (스펙 §5 P0)
        tracker: UsageTracker = state["usage"]
        if not tracker.indexing_allowed():
            # 스펙 §10: 상한 도달 시 요약·인덱싱만 일시정지 — 검색·답변 경로는 이 게이트를
            # 지나지 않으므로 계속 동작한다. 다음 날이 되면 카운터가 롤오버되어 자동 재개.
            entry["ok"] = False
            entry["error"] = (
                f"일일 API 호출 상한({tracker.daily_limit}건) 도달 — 오늘 누적 "
                f"{tracker.today_total()}건. 요약·인덱싱을 일시정지합니다 (검색·답변은 계속 가능)."
            )
            _logger.warning("%s 동기화 건너뜀: %s", source, entry["error"])
            state["log"].insert(0, entry)
            del state["log"][200:]
            return entry
        try:
```

`chat` 엔드포인트 시작부(`question = ...` 앞)에 추가:

```python
        state["usage"].record("answer")
```

`config.example.yaml`에 추가:

```yaml
limits:
  daily_api_calls: 0   # 일일 API 호출 상한 (0 = 무제한). 도달 시 요약·인덱싱만 일시정지, 검색·답변은 유지
```

`README.md` "개발" 섹션 앞에 추가:

```markdown
## 비용 통제

- API 호출(임베딩 embed / 요약 summary / 비전 vision / 답변 answer)은 `data_dir/usage.json`에
  일자별로 집계되고 호출 때마다 로그로 남는다 (최근 30일 보관). 카운트는 논리 호출 단위라
  실제 API 호출보다 적게 셀 수 있다 — 임베딩 1건은 내부적으로 100건 배치 여러 번일 수 있고,
  답변 1건은 도구 라운드에 따라 스트림 호출 최대 4회다.
- `config.yaml`의 `limits.daily_api_calls`로 일일 상한을 걸 수 있다 (기본 0 = 무제한).
  상한 도달 시 **요약·인덱싱(동기화)만 일시정지**되고 검색·채팅 답변은 계속 동작한다 —
  동기화 로그에 안내가 남고, 다음 날 자동 재개된다.
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_config.py tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` 전체 green (기존 248 + 신규)

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/config.py src/llmsearch/web/app.py config.example.yaml README.md tests/test_config.py tests/test_web.py
git commit -m "feat: 일일 API 상한 웹 통합 — run_sync 게이트·answer 기록·limits 설정 (스펙 §10 P2)"
```

---

## M5 수동 체크리스트 (실환경 — 머지 후 사용자 확인)

1. `config.yaml`에 `limits.daily_api_calls: 5` 같은 작은 값을 걸고 동기화 반복 → 로그 탭에 "일일 API 호출 상한 도달" 안내가 뜨고 채팅은 계속 동작하는지 확인
2. `data_dir/usage.json`에 일자별 종류별 카운트가 쌓이는지 확인 후 상한을 0으로 되돌리기
