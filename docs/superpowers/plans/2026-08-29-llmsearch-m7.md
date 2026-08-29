# llmsearch M7 — 검색 품질·평가 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스펙 M7 — 출처 카드 발췌(`Hit.snippet`), 검색 툴 스키마 현행화(6소스·sender)+Claude 필터 고지, 채팅 필터(검증·선검색 강제·툴 기본값)+`ask()` 오류 표시, 골든 평가(`evaluate` 확장·CLI 기본값·GUI 편집/실행), E2E.

**Architecture:** 필터 적용은 `web/app.py`의 `_apply_filters`(래퍼)에서만 — 사전 검색은 `search_fn(question)`이라 필터가 강제되고, 툴 호출은 명시 인자가 우선하되 None/falsy는 필터로 채운다. `llm.py`는 툴 스키마·`sender` 전달·`filters_note` 키워드만 추가(답변 루프 불변). 골든 파싱(`parse_golden`)은 CLI·API 공용. 평가 실행은 자체 읽기 커넥션으로 재구축과 격리.

**Tech Stack:** Python 3.12, FastAPI, SQLite, PyYAML(기존 의존), Playwright E2E

**Spec:** `docs/superpowers/specs/2026-08-29-llmsearch-m7-design.md`

## Global Constraints

- 필터는 선검색 강제·툴 검색 기본값(None/`[]`/`""`만 채움) — `llm.py` 답변 루프 로직 불변
- `sender` + `outlook_mail` 미포함 `source_filter` → 400; 필터 검증은 `record("answer")` **이전**
- `Hit.snippet`은 최고 RRF 청크에서 헤더 `f"[{title} | {updated_at[:10]}] "`를 `removeprefix`로 제거·공백 정규화·200자; LLM 컨텍스트(`_hits_block`) 무변경
- 골든: `yaml.safe_load`만, None=빈 목록, `GOLDEN_MAX_CASES = 50`, 원자적 저장, 256KB; run은 자체 읽기 커넥션·`evaluate_lock`·재구축 중 409·임베딩 실패 502(클래스명만)
- UI 동적 값은 `esc()`/`textContent`만; 상태 변경 API는 `local_origin_only`
- 웹 테스트 `TestClient(base_url="http://127.0.0.1")`; embed 카운트 단언 테스트는 스위트 내 유일한 질문 문자열(`search._QUERY_CACHE`)
- `src/llmsearch/eval/golden.py`는 **탭 들여쓰기 레거시 파일** — 수정 시 주변과 동일하게 탭 유지(그 외 파일은 4칸 공백)
- 기존 321 테스트 무변경 통과, 태스크마다 전체 green; E2E 기존 66건 기대값 무변경, 신규는 9.9 뒤 `# 10.` 앞
- 커밋 메시지 한국어, `feat:`/`test:` 접두사

---

### Task 1: `Hit.snippet` — 최고 청크 발췌 + 출처 카드 표시

**Files:**
- Modify: `src/llmsearch/models.py`, `src/llmsearch/search.py`, `src/llmsearch/web/static/index.html`
- Test: `tests/test_search.py` (추가)

**Interfaces:**
- Produces: `Hit.snippet: str = ""`(말단 기본값 — 위치 인자 8개 생성자 호환); `search.SNIPPET_CAP = 200`; `search._snippet(text, title, updated_at) -> str`; 카드 `<div class="snip">`. Task 7 E2E가 `.snip`을 확인.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_search.py` 끝에 추가:

```python
def test_snippet_strips_header_and_caps(tmp_path: Path):
    conn = setup_index(tmp_path)
    hits = search.search(conn, EMB, "프로젝트A 킥오프 회의록")
    top = hits[0]
    assert top.source_id == "kickoff.md"
    assert top.snippet and not top.snippet.startswith("[")  # 청크 헤더 제거
    assert top.snippet.startswith("프로젝트A 킥오프 회의록")
    assert len(top.snippet) <= search.SNIPPET_CAP
    assert "\n" not in top.snippet  # 공백 정규화


def test_snippet_header_with_brackets_and_pipes(tmp_path: Path):
    conn = db.open_db(tmp_path / "b.db")
    indexer.index_documents(conn, [
        Document("jira", "PROJ-1", "[PROJ-1] 검색 | 버그 수정", "재현 절차: 검색창에 입력 시 500. " * 20,
                 "https://jira/PROJ-1", datetime(2026, 8, 12)),
    ], EMB)
    hit = search.search(conn, EMB, "검색 500 재현")[0]
    assert hit.snippet.startswith("재현 절차")
    assert search._snippet("[t | 2026-01-01] 본문", "t", "2026-01-01T10:00:00") == "본문"
    assert search._snippet("헤더 없음", "t", "2026-01-01") == "헤더 없음"
    assert len(search._snippet("[t | 2026-01-01] " + "가 " * 500, "t", "2026-01-01")) == search.SNIPPET_CAP


def test_hit_positional_constructor_compat():
    from llmsearch.models import Hit

    h = Hit("notes", "a.md", "제목", "/n/a.md", "2026-08-01", True, 1.0, "본문")
    assert h.snippet == ""
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_search.py -v -k "snippet or positional"`
Expected: FAIL — `AttributeError: snippet`, `SNIPPET_CAP`

- [ ] **Step 3: 구현**

`src/llmsearch/models.py` `Hit` 끝에:

```python
    snippet: str = ""       # 최고 점수 청크 발췌 (헤더 제거, 200자) — 출처 카드 표시용, LLM 컨텍스트 아님
```

`src/llmsearch/search.py` — 상수·헬퍼(`EXCERPT_CAP` 아래):

```python
SNIPPET_CAP = 200


def _snippet(text: str, title: str, updated_at: str) -> str:
    """청크 헤더 `[제목 | YYYY-MM-DD] `를 재구성해 제거(정규식 금지 — 제목에 ]·| 가능), 공백 정규화, 200자."""
    header = f"[{title} | {updated_at[:10]}] "
    body = text.removeprefix(header)
    return " ".join(body.split())[:SNIPPET_CAP]
```

`search()` 결과 루프의 `hits.append(...)` 직전에 최고 청크 텍스트를 구해 전달:

```python
        best = doc_best_chunk[doc_id]
        best_text = next((t for c, t in chunk_rows if c == best), "")
        hits.append(Hit(stype, sid, title, url, updated, bool(cidx), doc_scores[doc_id], full,
                        _snippet(best_text, title, updated)))
```

`index.html` — CSS에 `.snip { color:#666; font-size:.85em; white-space:normal; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }` 추가. 카드 템플릿의 `<br>` 뒤(코드 줄 앞)에 `${h.snippet ? `<div class="snip">${esc(h.snippet)}</div>` : ''}` 삽입.

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_search.py tests/test_llm.py -v` → PASS, `./.venv/bin/pytest -q` → 321 + 3 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/models.py src/llmsearch/search.py src/llmsearch/web/static/index.html tests/test_search.py
git commit -m "feat: 출처 카드 발췌 — Hit.snippet(최고 청크·헤더 제거·200자) (스펙 M7 §3)"
```

---

### Task 2: 검색 툴 스키마 현행화 + `sender` 전달 + `filters_note` 고지

**Files:**
- Modify: `src/llmsearch/llm.py`
- Test: `tests/test_llm.py` (추가)

**Interfaces:**
- Produces: `_SEARCH_TOOL` enum 6소스 + `sender`; `Answerer.answer_stream(question, history, search_fn, filters_note: str = "")`; `FakeAnswerer.last_filters_note`; Claude 툴 루프가 `sender=args.get("sender")` 전달, `filters_note`를 사전 검색 블록 앞에 삽입. Task 3이 `filters_note=`로 호출.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_llm.py` 끝에 추가:

```python
import types


def test_search_tool_schema_covers_all_sources_and_sender():
    from llmsearch.llm import _SEARCH_TOOL

    props = _SEARCH_TOOL["input_schema"]["properties"]
    assert props["source_filter"]["items"]["enum"] == [
        "notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira"]
    assert props["sender"]["type"] == "string"
    assert "메일" in _SEARCH_TOOL["description"]


def test_fake_answerer_accepts_filters_note():
    a = FakeAnswerer()
    list(a.answer_stream("q", [], lambda *x, **k: [HIT], filters_note="(사용자 필터 적용: 소스=notes)"))
    assert a.last_filters_note == "(사용자 필터 적용: 소스=notes)"


class _Stream:
    def __init__(self, texts, final):
        self._texts, self._final = texts, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self._texts)

    def get_final_message(self):
        return self._final


class _Messages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        texts, final = self.responses.pop(0)
        return _Stream(texts, final)


def _tool_use_then_end(tool_input: dict):
    first = types.SimpleNamespace(stop_reason="tool_use", content=[
        types.SimpleNamespace(type="tool_use", id="t1", input=tool_input)])
    second = types.SimpleNamespace(stop_reason="end_turn", content=[])
    return [(["검색 중..."], first), (["답변 [1]"], second)]


def test_claude_tool_loop_passes_sender_and_filters_note(monkeypatch):
    import sys
    from llmsearch.llm import ClaudeAnswerer

    fake_mod = types.ModuleType("anthropic")
    messages = _Messages(_tool_use_then_end(
        {"query": "김철수 메일", "sender": "kim@corp.com", "source_filter": []}))
    fake_mod.Anthropic = lambda: types.SimpleNamespace(messages=messages)
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    calls = []

    def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
        calls.append((query, source_filter, date_from, date_to, sender))
        return [HIT]

    a = ClaudeAnswerer()
    events = list(a.answer_stream("김철수가 보낸 결정 사항?", [], search_fn,
                                  filters_note="(사용자 필터 적용: 소스=outlook_mail)"))
    assert calls[0] == ("김철수가 보낸 결정 사항?", None, None, None, None)     # 사전 검색: 키워드 미지정
    assert calls[1] == ("김철수 메일", [], None, None, "kim@corp.com")          # 툴 호출: sender 전달
    first_user = messages.calls[0]["messages"][0]["content"]
    assert first_user.index("(사용자 필터 적용") < first_user.index("사전 검색 결과")
    assert "".join(e["text"] for e in events if e["type"] == "text") == "검색 중...답변 [1]"
    assert len(messages.calls) == 2


def test_claude_no_note_when_empty(monkeypatch):
    import sys
    from llmsearch.llm import ClaudeAnswerer

    fake_mod = types.ModuleType("anthropic")
    messages = _Messages([(["끝"], types.SimpleNamespace(stop_reason="end_turn", content=[]))])
    fake_mod.Anthropic = lambda: types.SimpleNamespace(messages=messages)
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    list(ClaudeAnswerer().answer_stream("q", [], lambda *x, **k: [HIT]))
    assert "사용자 필터" not in messages.calls[0]["messages"][0]["content"]
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_llm.py -v -k "schema or filters_note or tool_loop or no_note"`
Expected: FAIL — enum 불일치, `TypeError: unexpected keyword 'filters_note'`, `AttributeError: last_filters_note`

- [ ] **Step 3: 구현**

`src/llmsearch/llm.py`:

```python
_SEARCH_TOOL = {
    "name": "search",
    "description": (
        "사내 통합 인덱스(로컬 문서 요약, 개인 메모, Outlook 메일·일정, Confluence, Jira)를 검색한다. "
        "첫 검색 결과가 부족하거나, 다른 표현·필터로 더 찾아야 할 때 호출하라. "
        "일정·날짜 관련 질문은 date_from/date_to 필터를, 특정 발신자의 메일은 sender 필터를 사용하라."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "검색 질의"},
            "source_filter": {"type": "array", "items": {"type": "string",
                "enum": ["notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira"]},
                "description": "소스 한정(선택)"},
            "date_from": {"type": "string", "description": "YYYY-MM-DD 이후(선택)"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD 이전(선택)"},
            "sender": {"type": "string", "description": "보낸 사람 이메일 (메일 전용, 선택)"},
        },
        "required": ["query"],
    },
}


class Answerer(Protocol):
    def answer_stream(self, question: str, history: list[dict], search_fn: SearchFn,
                      filters_note: str = "") -> Iterator[dict]: ...

    def update_rules(self, sections: dict[str, str]) -> None: ...


class FakeAnswerer:
    def __init__(self):
        self.rules: dict[str, str] = {}  # 마지막 update_rules 값 — 테스트 관찰용
        self.last_filters_note = ""      # 마지막 filters_note — 테스트 관찰용

    def update_rules(self, sections: dict[str, str]) -> None:
        self.rules = dict(sections)

    def answer_stream(self, question, history, search_fn, filters_note: str = "") -> Iterator[dict]:
        self.last_filters_note = filters_note
        ...(기존 본문 그대로)
```

`ClaudeAnswerer.answer_stream(self, question, history, search_fn, filters_note: str = "")` — 사전 검색 메시지 조립을 교체:

```python
            note = f"{filters_note}\n\n" if filters_note else ""
            messages = list(history) + [{
                "role": "user",
                "content": f"질문: {question}\n\n{note}사전 검색 결과:\n{_hits_block(all_hits)}",
            }]
```

툴 호출 `search_fn(...)`에 `sender=args.get("sender"),` 추가.

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_llm.py tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/llm.py tests/test_llm.py
git commit -m "feat: 검색 툴 스키마 현행화(6소스·sender)·filters_note 고지·스트림 Fake 테스트 (스펙 M7 §2)"
```

---

### Task 3: `/api/chat` 필터 — 검증·정규화·`_apply_filters`·고지 전달

**Files:**
- Modify: `src/llmsearch/web/app.py`
- Test: `tests/test_web.py` (추가)

**Interfaces:**
- Consumes: Task 2 `filters_note` 키워드, `FakeAnswerer.last_filters_note`
- Produces: 모듈 함수 `_validate_filters(raw) -> dict`(키 `source_filter/date_from/date_to/sender`, 위반 `HTTPException(400)`), `_apply_filters(search_fn, filters)`, `_filters_note(filters) -> str`; `/api/chat` 페이로드 `filters`. Task 4 UI가 페이로드 형식을 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py` 끝에 추가:

```python
def make_app_mixed(tmp_path: Path, monkeypatch) -> TestClient:
    """notes 1 + local_docs 1 — 소스 필터가 실제로 결과를 가르는지 보기 위한 구성."""
    from llmsearch.connectors import local_docs

    monkeypatch.setattr(local_docs, "extract_text", lambda p: "프로젝트A 아키텍처 설계 본문 " * 10)
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "kick.md").write_text("# 프로젝트A 킥오프\n프로젝트A 일정 확정", encoding="utf-8")
    watch = tmp_path / "watch"; watch.mkdir()
    (watch / "설계.pptx").write_bytes(b"x")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], watch_folders=[watch], projects=["프로젝트A"])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.post("/api/sync/notes"); client.post("/api/sync/local_docs")
    return client


def _sources_of(body: str) -> list[str]:
    line = next(l for l in body.splitlines() if l.startswith("data: [") or l.startswith("data: []"))
    return [h["source_type"] for h in json.loads(line[len("data: "):])]


def test_chat_source_filter_forces_presearch(tmp_path: Path, monkeypatch):
    client = make_app_mixed(tmp_path, monkeypatch)
    with client.stream("POST", "/api/chat", json={"question": "프로젝트A 필터 검증 질의", "history": [],
                                                   "filters": {"source_filter": ["notes"]}}) as r:
        body = "".join(r.iter_text())
    assert _sources_of(body) == ["notes"]
    with client.stream("POST", "/api/chat", json={"question": "프로젝트A 필터 없음 질의", "history": []}) as r:
        body = "".join(r.iter_text())
    assert set(_sources_of(body)) == {"notes", "local_docs"}
    assert client.app.state.llmsearch["answerer"].last_filters_note == ""


def test_chat_filters_note_delivered_and_normalized(tmp_path: Path, monkeypatch):
    client = make_app_mixed(tmp_path, monkeypatch)
    client.post("/api/chat", json={"question": "고지 전달 질의", "history": [],
                                   "filters": {"source_filter": ["local_docs", "notes", "notes"],
                                               "date_from": "2026-08-01", "date_to": "", "sender": ""}})
    note = client.app.state.llmsearch["answerer"].last_filters_note
    assert note.startswith("(사용자 필터 적용: 소스=notes,local_docs, 기간=2026-08-01~")  # SOURCES 순서·중복 제거
    assert "빈 배열" in note


def test_chat_filter_validation_400_and_no_answer_count(tmp_path: Path, monkeypatch):
    client = make_app_mixed(tmp_path, monkeypatch)
    before = client.app.state.llmsearch["usage"].today_by_kind().get("answer", 0)
    bad = [
        {"source_filter": "notes"}, {"source_filter": ["slack"]}, {"source_filter": [1]},
        {"date_from": "2026-13-45"}, {"date_to": 20260801}, {"sender": "x" * 201},
        {"sender": "kim@corp.com", "source_filter": ["notes"]},
    ]
    for f in bad:
        r = client.post("/api/chat", json={"question": "q", "history": [], "filters": f})
        assert r.status_code == 400, f
    assert client.post("/api/chat", json={"question": "q", "history": [], "filters": "x"}).status_code == 400
    assert client.app.state.llmsearch["usage"].today_by_kind().get("answer", 0) == before  # 400은 answer 미계상
    ok = client.post("/api/chat", json={"question": "sender 단독 허용 질의", "history": [],
                                        "filters": {"sender": " kim@corp.com ", "source_filter": []}})
    assert ok.status_code == 200
    assert "발신자=kim@corp.com" in client.app.state.llmsearch["answerer"].last_filters_note


def test_apply_filters_fills_only_missing_args():
    from llmsearch.web.app import _apply_filters

    calls = []

    def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
        calls.append((source_filter, date_from, date_to, sender))
        return []

    f = {"source_filter": ["notes"], "date_from": "2026-08-01", "date_to": None, "sender": "a@b"}
    wrapped = _apply_filters(search_fn, f)
    wrapped("q")                                                   # 선검색: 전부 필터
    wrapped("q", source_filter=["jira"], date_from="", date_to="2026-09-01", sender=None)  # 툴: 명시값 우선, falsy 채움
    assert calls == [(["notes"], "2026-08-01", None, "a@b"), (["jira"], "2026-08-01", "2026-09-01", "a@b")]
    assert _apply_filters(search_fn, {"source_filter": None, "date_from": None, "date_to": None, "sender": None}) is search_fn
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v -k "filter"`
Expected: FAIL — `ImportError: _apply_filters`; 필터가 무시되어 sources에 local_docs 포함; 400이어야 할 요청이 200

- [ ] **Step 3: 구현**

`src/llmsearch/web/app.py` — import `from datetime import date, datetime`. 모듈 함수(`local_origin_only` 아래):

```python
_FILTER_KEYS = ("source_filter", "date_from", "date_to", "sender")


def _validate_filters(raw) -> dict:
    """/api/chat `filters` 검증·정규화 (스펙 M7 §2). 위반은 400 — record("answer") 이전에 호출한다."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "filters는 객체여야 합니다")
    out: dict = {k: None for k in _FILTER_KEYS}
    sf = raw.get("source_filter")
    if sf:
        if not isinstance(sf, list) or not all(isinstance(s, str) for s in sf):
            raise HTTPException(400, "source_filter는 문자열 리스트여야 합니다")
        unknown = [s for s in sf if s not in SOURCES]
        if unknown:
            raise HTTPException(400, f"알 수 없는 소스: {', '.join(unknown)}")
        out["source_filter"] = [s for s in SOURCES if s in sf]  # 중복 제거 + SOURCES 순서 정규화 (길이 ≤ 6)
    for key in ("date_from", "date_to"):
        v = raw.get(key)
        if v:
            if not isinstance(v, str):
                raise HTTPException(400, f"{key}는 YYYY-MM-DD 문자열이어야 합니다")
            try:
                date.fromisoformat(v)
            except ValueError:
                raise HTTPException(400, f"{key} 형식 오류: YYYY-MM-DD")
            out[key] = v  # 자정 경계 보정은 search.search가 한다
    sender = raw.get("sender")
    if sender:
        if not isinstance(sender, str) or len(sender.strip()) > 200:
            raise HTTPException(400, "sender는 200자 이하 문자열이어야 합니다")
        sender = sender.strip()
        if sender:
            if out["source_filter"] and "outlook_mail" not in out["source_filter"]:
                raise HTTPException(400, "발신자 필터는 메일 소스에서만 동작합니다 — 소스에서 outlook_mail을 "
                                         "선택하거나 소스 선택을 비우세요")
            out["sender"] = sender
    return out


def _apply_filters(search_fn, filters: dict):
    """선검색은 강제, 툴 검색은 기본값 — None/빈 값인 인자만 필터로 채운다 (스펙 M7 §2).

    answer_stream의 사전 검색은 search_fn(question)이라 전부 채워지고(강제), Claude 툴 호출은
    명시한 값이 우선한다. []·""도 미지정으로 본다 — Claude가 빈 배열로 사용자 필터를 조용히
    해제하지 못하게.
    """
    if not any(filters.get(k) for k in _FILTER_KEYS):
        return search_fn

    def wrapped(query, source_filter=None, date_from=None, date_to=None, sender=None):
        return search_fn(query, source_filter=source_filter or filters["source_filter"],
                         date_from=date_from or filters["date_from"],
                         date_to=date_to or filters["date_to"],
                         sender=sender or filters["sender"])
    return wrapped


def _filters_note(filters: dict) -> str:
    parts = []
    if filters.get("source_filter"):
        parts.append("소스=" + ",".join(filters["source_filter"]))
    if filters.get("date_from") or filters.get("date_to"):
        parts.append(f"기간={filters.get('date_from') or ''}~{filters.get('date_to') or ''}")
    if filters.get("sender"):
        parts.append("발신자=" + filters["sender"])
    if not parts:
        return ""
    return ("(사용자 필터 적용: " + ", ".join(parts) + ". 다른 범위가 필요하면 search 툴에 값을 명시하라 — "
            "빈 배열·빈 문자열은 무시되며, 전체 소스를 검색하려면 6개 소스를 모두 나열하라)")
```

`/api/chat`:

```python
    @app.post("/api/chat", dependencies=[Depends(local_origin_only)])
    def chat(payload: dict):
        _require_db()
        filters = _validate_filters(payload.get("filters"))  # 400은 answer 계상 전에
        state["usage"].record("answer")
        question = payload.get("question", "")
        history = payload.get("history", [])

        def raw_search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
            return search.search(state["read_conn"], embedder, query, source_filter=source_filter,
                                 date_from=date_from, date_to=date_to, sender=sender)

        search_fn = _apply_filters(raw_search_fn, filters)
        note = _filters_note(filters)

        def event_stream():
            for ev in state["answerer"].answer_stream(question, history, search_fn, filters_note=note):
                ...(기존 그대로)
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/app.py tests/test_web.py
git commit -m "feat: /api/chat 필터 — 검증·정규화·선검색 강제/툴 기본값·Claude 고지 (스펙 M7 §2)"
```

---

### Task 4: 채팅 필터 UI + `ask()` 오류 표시

**Files:**
- Modify: `src/llmsearch/web/static/index.html`
- Test: `tests/test_web.py` (추가 1건)

**Interfaces:**
- Consumes: Task 3 페이로드 `filters`
- Produces: `#filters`(details), `.srcChk` 체크박스 6개(`data-src`), `#fDateFrom`, `#fDateTo`, `#fSender`, `.filters-note`, JS `currentFilters()`, `filtersLabel(f)`; `ask()`가 비-2xx에 `⚠️ detail` 표시·history 미push. Task 7 E2E가 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_chat_filter_ui_in_index(tmp_path: Path):
    client = make_app(tmp_path)
    html = client.get("/").text
    for needle in ('id="filters"', 'id="fDateFrom"', 'id="fDateTo"', 'id="fSender"', "filters-note",
                   "입력 시 메일만 검색됩니다", "resp.ok"):
        assert needle in html, needle
```

- [ ] **Step 2: 실패 확인** — `./.venv/bin/pytest tests/test_web.py -v -k filter_ui` → FAIL

- [ ] **Step 3: 구현**

`index.html` 채팅 폼 뒤:

```html
  <details id="filters"><summary>필터</summary>
    <div id="srcFilters"></div>
    <label>기간 <input type="date" id="fDateFrom"> ~ <input type="date" id="fDateTo"></label>
    <label>발신자 <input id="fSender" placeholder="kim@corp.com" title="입력 시 메일만 검색됩니다"></label>
    <small>입력 시 메일만 검색됩니다</small>
  </details>
```

CSS: `.filters-note { color:#666; font-size:.85em; margin:.2rem 0 .4rem; }`. JS (`ask` 위):

```js
const SOURCES_UI = ['notes', 'local_docs', 'outlook_mail', 'outlook_cal', 'confluence', 'jira'];
document.getElementById('srcFilters').replaceChildren(...SOURCES_UI.map(s => {
  const l = document.createElement('label'); const c = document.createElement('input');
  c.type = 'checkbox'; c.className = 'srcChk'; c.dataset.src = s;
  l.append(c, ' ' + s + ' '); return l;
}));
function currentFilters() {
  const srcs = [...document.querySelectorAll('.srcChk:checked')].map(c => c.dataset.src);
  const v = id => document.getElementById(id).value.trim() || null;
  return {source_filter: srcs.length ? srcs : null, date_from: v('fDateFrom'), date_to: v('fDateTo'), sender: v('fSender')};
}
function filtersLabel(f) {
  const parts = [];
  if (f.source_filter) parts.push('소스=' + f.source_filter.join(','));
  if (f.date_from || f.date_to) parts.push('기간=' + (f.date_from || '') + '~' + (f.date_to || ''));
  if (f.sender) parts.push('발신자=' + f.sender);
  return parts.length ? '필터(첫 검색 기준): ' + parts.join(' · ') + ' — 답변 근거는 Claude의 추가 검색으로 넓어질 수 있습니다' : '';
}
```

`ask()` 수정: `qDiv` 추가 뒤에

```js
  const filters = currentFilters();
  const label = filtersLabel(filters);
  if (label) { const n = document.createElement('div'); n.className = 'filters-note'; n.textContent = label; box.appendChild(n); }
```

fetch body를 `JSON.stringify({question: q, history, filters})`로; fetch 직후:

```js
  if (!resp.ok) {  // 400(필터 검증)·503(스키마 불일치) — 무증상 실패·history 오염 방지
    let d = {}; try { d = await resp.json(); } catch {}
    answerDiv.textContent = '⚠️ ' + (d.detail || ('HTTP ' + resp.status));
    return;
  }
```

- [ ] **Step 4: 통과 확인** — `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/static/index.html tests/test_web.py
git commit -m "feat: 채팅 필터 UI(소스·기간·발신자)·필터 표시 줄·ask() 오류 표시 (스펙 M7 §2)"
```

---

### Task 5: `evaluate` 질문별 순위 + `parse_golden` + CLI `--golden` 기본값

**Files:**
- Modify: `src/llmsearch/eval/golden.py` (탭 들여쓰기 유지)
- Test: `tests/test_golden.py` (추가)

**Interfaces:**
- Produces: `golden.GOLDEN_MAX_CASES = 50`; `golden.parse_golden(text: str) -> list[dict]`(`ValueError` 메시지 한국어); `evaluate(...)` 반환에 `cases: [{question, expected, rank, got}]` 추가, `misses`는 `rank is None` 파생(형식 유지); `main()`의 `--golden` 선택(기본 `data_dir/golden.yaml`), 파일 없음/파싱 실패 시 메시지 + exit 1. Task 6 API가 `parse_golden`·`evaluate` 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_golden.py` 끝에 추가 (이 파일도 탭 들여쓰기):

```python
def test_evaluate_reports_rank_per_case(tmp_path: Path):
	from llmsearch.eval.golden import evaluate

	conn = db.open_db(tmp_path / "r.db")
	emb = FakeEmbeddings(dim=768)
	indexer.index_documents(conn, [
		Document("notes", "kick.md", "프로젝트A 킥오프", "프로젝트A 킥오프 회의록 8월 1일", "/n/kick.md", datetime(2026, 8, 1)),
		Document("notes", "lunch.md", "점심", "김치찌개", "/n/lunch.md", datetime(2026, 8, 1)),
	], emb)
	report = evaluate(conn, emb, [
		{"question": "프로젝트A 킥오프 언제?", "expect_source_id": "kick.md"},
		{"question": "존재하지 않는 주제 XYZQW", "expect_source_id": "none.md"},
	])
	assert report["cases"][0]["rank"] == 1 and report["cases"][0]["got"][0] == "kick.md"
	assert report["cases"][1]["rank"] is None
	assert report["misses"] == [{"question": "존재하지 않는 주제 XYZQW", "expected": "none.md", "got": report["cases"][1]["got"]}]
	assert set(report) == {"total", "hit_at_3", "rate", "misses", "cases"}


def test_parse_golden_rules():
	import pytest
	from llmsearch.eval.golden import GOLDEN_MAX_CASES, parse_golden

	assert parse_golden("") == [] and parse_golden("# 주석만\n") == []
	assert parse_golden("- question: q\n  expect_source_id: a.md\n") == [{"question": "q", "expect_source_id": "a.md"}]
	for bad in ("question: q\n", "- q\n", "- question: q\n", "- question: ''\n  expect_source_id: a\n", "- {question: q, expect_source_id: 1}\n"):
		with pytest.raises(ValueError):
			parse_golden(bad)
	with pytest.raises(ValueError, match=str(GOLDEN_MAX_CASES)):
		parse_golden("".join(f"- question: q{i}\n  expect_source_id: a\n" for i in range(GOLDEN_MAX_CASES + 1)))
	with pytest.raises(ValueError):
		parse_golden("- question: [unclosed\n")


def test_main_default_golden_path_and_missing_file(tmp_path: Path, monkeypatch, capsys):
	import llmsearch.eval.golden as g
	from llmsearch.config import Config

	monkeypatch.setattr(g, "load_dotenv", lambda *a, **k: None)
	monkeypatch.setattr(g, "load_config", lambda p: Config(data_dir=tmp_path / "data"))
	monkeypatch.setattr("sys.argv", ["golden", "--config", "c.yaml"])
	import pytest
	with pytest.raises(SystemExit) as ei:
		g.main()
	assert ei.value.code == 1 and "golden.yaml이 없습니다" in capsys.readouterr().out
```

- [ ] **Step 2: 실패 확인** — `./.venv/bin/pytest tests/test_golden.py -v` → FAIL (`cases` 키 없음, `parse_golden` ImportError, `--golden` required)

- [ ] **Step 3: 구현**

`src/llmsearch/eval/golden.py` (탭 들여쓰기):

```python
GOLDEN_MAX_CASES = 50  # 1클릭 임베딩 예산 상한 (스펙 M7 §4)


def parse_golden(text: str) -> list[dict]:
	"""golden.yaml 본문 → 케이스 목록. CLI·API 공용. 위반은 ValueError(한국어 사유)."""
	try:
		data = yaml.safe_load(text)
	except yaml.YAMLError as exc:
		raise ValueError(f"YAML 파싱 실패: {exc}") from exc
	if data is None:
		return []
	if not isinstance(data, list):
		raise ValueError("golden.yaml은 목록([- question: ..., expect_source_id: ...])이어야 합니다")
	if len(data) > GOLDEN_MAX_CASES:
		raise ValueError(f"케이스가 {GOLDEN_MAX_CASES}건을 초과합니다 ({len(data)}건)")
	cases = []
	for i, item in enumerate(data, start=1):
		if not isinstance(item, dict):
			raise ValueError(f"{i}번째 항목이 객체가 아닙니다")
		q, e = item.get("question"), item.get("expect_source_id")
		if not isinstance(q, str) or not q.strip() or not isinstance(e, str) or not e.strip():
			raise ValueError(f"{i}번째 항목: question·expect_source_id는 비어 있지 않은 문자열이어야 합니다")
		cases.append({"question": q.strip(), "expect_source_id": e.strip()})
	return cases


def evaluate(conn, embedder, cases: list[dict]) -> dict:
	results = []
	for case in cases:
		found = [h.source_id for h in search.search(conn, embedder, case["question"], k=3)]
		rank = next((i + 1 for i, sid in enumerate(found) if _matches(case["expect_source_id"], sid)), None)
		results.append({"question": case["question"], "expected": case["expect_source_id"], "rank": rank, "got": found})
	total = len(cases)
	hits_at_3 = sum(1 for r in results if r["rank"] is not None)
	misses = [{"question": r["question"], "expected": r["expected"], "got": r["got"]} for r in results if r["rank"] is None]
	return {"total": total, "hit_at_3": hits_at_3,
			"rate": hits_at_3 / total if total else 0.0, "misses": misses, "cases": results}
```

`main()`: `parser.add_argument("--golden", type=Path, default=None)`; `cfg = load_config(...)` 뒤:

```python
	golden_path = args.golden or (cfg.data_dir / "golden.yaml")
	if not golden_path.exists():
		print(f"golden.yaml이 없습니다: {golden_path}")
		sys.exit(1)
	try:
		cases = parse_golden(golden_path.read_text(encoding="utf-8"))
	except ValueError as exc:
		print(f"golden.yaml 오류: {exc}")
		sys.exit(1)
	if not cases:
		print("golden.yaml이 비어 있습니다")
		sys.exit(1)
```

(기존 `yaml.safe_load`·빈 목록 처리 줄은 제거; `GeminiEmbeddings` 지연 import·주석·출력은 유지.)

- [ ] **Step 4: 통과 확인** — `./.venv/bin/pytest tests/test_golden.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/eval/golden.py tests/test_golden.py
git commit -m "feat: 골든 평가 — 질문별 순위(cases)·parse_golden 공용 파서·CLI --golden 기본 경로 (스펙 M7 §4)"
```

---

### Task 6: 골든 평가 API + 설정 탭 UI

**Files:**
- Modify: `src/llmsearch/web/app.py`, `src/llmsearch/web/static/index.html`
- Test: `tests/test_web.py` (추가)

**Interfaces:**
- Consumes: Task 5 `parse_golden`/`evaluate`/`GOLDEN_MAX_CASES`; `_require_db`, `local_origin_only`, `state["rebuilding"]`
- Produces: `GOLDEN_TEMPLATE`; `GET /api/eval/golden` → `{text, path, count}`; `PUT /api/eval/golden {text}` → `{ok, count}`; `POST /api/eval/golden/run` → `{total, hit_at_3, rate, target, pass, cases}`; `state["evaluating"]`, `state["evaluate_lock"]`; UI `#goldenText`, `#saveGoldenBtn`, `#runGoldenBtn`, `#goldenStatus`, `#goldenResult`(헤더 `#goldenHeader` + 표 `#goldenTable`), JS `loadGolden/saveGolden/runGolden`. Task 7 E2E가 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
GOLDEN_TWO = ("- question: 프로젝트A 킥오프 언제?\n  expect_source_id: kick.md\n"
              "- question: 존재하지 않는 주제 XYZQW\n  expect_source_id: none.md\n")


def test_golden_get_template_and_put(tmp_path: Path):
    client = make_app(tmp_path)
    g = client.get("/api/eval/golden").json()
    assert g["count"] == 0 and g["text"].startswith("#") and g["path"].endswith("golden.yaml")
    assert client.put("/api/eval/golden", json={"text": g["text"]}).json() == {"ok": True, "count": 0}  # 템플릿 저장 OK
    r = client.put("/api/eval/golden", json={"text": GOLDEN_TWO})
    assert r.status_code == 200 and r.json()["count"] == 2
    path = client.app.state.llmsearch["config"].data_dir / "golden.yaml"
    assert path.read_text(encoding="utf-8") == GOLDEN_TWO and not path.with_name("golden.yaml.tmp").exists()
    assert client.get("/api/eval/golden").json()["count"] == 2
    assert client.put("/api/eval/golden", json={"text": "question: q\n"}).status_code == 400
    assert client.put("/api/eval/golden", json={"text": 5}).status_code == 400
    assert client.put("/api/eval/golden", json={"text": "- question: [x\n"}).status_code == 400
    assert client.put("/api/eval/golden", json={"text": GOLDEN_TWO},
                      headers={"Origin": "http://evil.example"}).status_code == 403
    assert path.read_text(encoding="utf-8") == GOLDEN_TWO  # 실패한 PUT은 파일 미변경


def test_golden_run_reports_rank_and_pass(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    assert client.post("/api/eval/golden/run", json={}).status_code == 400  # 파일 없음
    client.put("/api/eval/golden", json={"text": GOLDEN_TWO})
    r = client.post("/api/eval/golden/run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and body["hit_at_3"] == 1 and body["rate"] == 0.5
    assert body["target"] == 0.7 and body["pass"] is False
    assert body["cases"][0]["rank"] == 1 and body["cases"][1]["rank"] is None
    assert client.app.state.llmsearch["usage"].today_by_kind()["embed"] >= 3  # notes 1 + 질의 2


def test_golden_run_refusals(tmp_path: Path):
    client = make_app(tmp_path)
    client.put("/api/eval/golden", json={"text": GOLDEN_TWO})
    state = client.app.state.llmsearch
    state["rebuilding"] = True
    assert client.post("/api/eval/golden/run", json={}).status_code == 409
    state["rebuilding"] = False
    state["evaluate_lock"].acquire()
    try:
        assert client.post("/api/eval/golden/run", json={}).status_code == 409
    finally:
        state["evaluate_lock"].release()
    assert client.post("/api/eval/golden/run", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    state["read_conn"] = None
    assert client.post("/api/eval/golden/run", json={}).status_code == 503


def test_golden_run_embedding_failure_hides_message(tmp_path: Path, monkeypatch):
    from llmsearch import search as search_mod

    client = make_app(tmp_path)
    client.put("/api/eval/golden", json={"text": GOLDEN_TWO})

    def boom(*a, **k):
        raise RuntimeError("secret api key sk-123")
    monkeypatch.setattr(search_mod, "search", boom)
    r = client.post("/api/eval/golden/run", json={})
    assert r.status_code == 502 and "RuntimeError" in r.json()["detail"] and "sk-123" not in r.text


def test_golden_ui_in_index(tmp_path: Path):
    client = make_app(tmp_path)
    html = client.get("/").text
    for needle in ('id="goldenText"', 'id="runGoldenBtn"', 'id="goldenTable"', "loadGolden()"):
        assert needle in html, needle
```

- [ ] **Step 2: 실패 확인** — `./.venv/bin/pytest tests/test_web.py -v -k golden` → FAIL (404, 요소 없음)

- [ ] **Step 3: 구현**

`app.py` — import `from ..eval.golden import GOLDEN_MAX_CASES, evaluate as golden_evaluate, parse_golden`. 상수:

```python
GOLDEN_TEMPLATE = (
    "# 골든 질문 세트 — 검색 상위 3위 적중률 측정 (목표 70%)\n"
    "# expect_source_id: 전체 경로 또는 경로 접미사(파일명). 동명 파일이 여러 폴더에 있으면 아무 쪽이나 적중.\n"
    "# - question: 프로젝트A 킥오프 언제?\n#   expect_source_id: kickoff.md\n"
)
```

`state` 리터럴에 `"evaluating": False, "evaluate_lock": threading.Lock(),`. 엔드포인트(`/api/usage` 뒤):

```python
    @app.get("/api/eval/golden")
    def golden_get():
        path = config.data_dir / "golden.yaml"
        text = path.read_text(encoding="utf-8") if path.exists() else GOLDEN_TEMPLATE
        try:
            count = len(parse_golden(text))
        except ValueError:
            count = 0
        return {"text": text, "path": str(path), "count": count}

    @app.put("/api/eval/golden", dependencies=[Depends(local_origin_only)])
    def golden_put(payload: dict):
        text = payload.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, "text는 문자열이어야 합니다")
        data = text.encode("utf-8")
        if len(data) > _RULES_MAX_BYTES:
            raise HTTPException(400, f"golden.yaml은 {_RULES_MAX_BYTES // 1024}KB 이하여야 합니다")
        try:
            cases = parse_golden(text)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        path = config.data_dir / "golden.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return {"ok": True, "count": len(cases)}

    @app.post("/api/eval/golden/run", dependencies=[Depends(local_origin_only)])
    def golden_run():
        """골든 세트 실행 — 검색 경로(상한 게이트 무관, usage에 embed 기록). 자체 읽기 커넥션으로 재구축과 격리."""
        _require_db()
        if state.get("rebuilding"):
            raise HTTPException(409, "인덱스 재구축이 진행 중입니다 — 완료 후 평가하세요")
        path = config.data_dir / "golden.yaml"
        if not path.exists():
            raise HTTPException(400, "golden.yaml에 질문을 먼저 작성하세요")
        try:
            cases = parse_golden(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not cases:
            raise HTTPException(400, "golden.yaml에 질문을 먼저 작성하세요")
        if not state["evaluate_lock"].acquire(blocking=False):
            raise HTTPException(409, "평가가 이미 진행 중입니다")
        state["evaluating"] = True
        conn = db.open_db(config.db_path)
        try:
            report = golden_evaluate(conn, embedder, cases)
        except Exception as exc:
            # 예외 메시지에 자격증명이 섞일 수 있어 클래스명만 노출 (CLAUDE.md 보안)
            _logger.exception("골든 평가 실패")
            raise HTTPException(502, f"임베딩 호출 실패: {type(exc).__name__}")
        finally:
            conn.close()
            state["evaluating"] = False
            state["evaluate_lock"].release()
        target = 0.7  # 상위 스펙 §1 성공 기준
        return {**report, "target": target, "pass": report["rate"] >= target}
```

`index.html` 설정 탭 운영 버튼 뒤:

```html
  <h3>검색 품질 평가 (golden.yaml) <small id="goldenPath"></small></h3>
  <p><small>expect_source_id는 전체 경로 또는 경로 접미사(파일명) — 동명 파일이 여러 폴더에 있으면 아무 쪽이나 적중으로 셉니다. 최대 50건.</small></p>
  <textarea id="goldenText" style="width:100%;height:30vh;font-family:monospace"></textarea><br>
  <button id="saveGoldenBtn" onclick="saveGolden()">저장</button>
  <button id="runGoldenBtn" onclick="runGolden()">평가 실행</button> <span id="goldenStatus"></span>
  <div id="goldenResult" style="display:none"><div id="goldenHeader"></div>
    <table id="goldenTable"><thead><tr><th>질문</th><th>기대</th><th>순위</th><th>상위 결과</th></tr></thead><tbody></tbody></table></div>
```

`show()`의 settings 분기에 `loadGolden();`. JS:

```js
async function loadGolden() {
  const g = await (await fetch('/api/eval/golden')).json();
  document.getElementById('goldenText').value = g.text;
  document.getElementById('goldenPath').textContent = g.path;
  document.getElementById('goldenStatus').textContent = `${g.count}건`;
}
async function saveGolden() {
  const r = await fetch('/api/eval/golden', {method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: document.getElementById('goldenText').value})});
  const d = await r.json();
  document.getElementById('goldenStatus').textContent = r.ok ? `저장됨 · ${d.count}건` : (d.detail || '저장 실패');
}
async function runGolden() {
  const g = await (await fetch('/api/eval/golden')).json();
  const u = await (await fetch('/api/usage')).json();
  let msg = `최대 ${g.count}건의 질의 임베딩 API 호출이 발생합니다.`;
  if (!u.indexing_allowed) msg += ' — 이미 일일 상한 도달 상태, 추가 소모됩니다';
  if (!confirm(msg + ' 계속할까요?')) return;
  const btn = document.getElementById('runGoldenBtn'); btn.disabled = true;
  try {
    const r = await fetch('/api/eval/golden/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await r.json();
    if (!r.ok) { document.getElementById('goldenStatus').textContent = d.detail || '평가 실패'; return; }
    document.getElementById('goldenHeader').textContent =
      `상위3 적중률 ${Math.round(d.rate * 100)}% (${d.hit_at_3}/${d.total}) — 목표 ${Math.round(d.target * 100)}% ${d.pass ? '✅' : '❌'}`;
    const tbody = document.querySelector('#goldenTable tbody');
    tbody.replaceChildren(...d.cases.map(c => {
      const tr = document.createElement('tr');
      for (const v of [c.question, c.expected, c.rank ?? '❌', c.got.join(', ')]) {
        const td = document.createElement('td'); td.textContent = String(v); tr.append(td);
      }
      return tr;
    }));
    document.getElementById('goldenResult').style.display = 'block';
  } finally { btn.disabled = false; }
}
```

- [ ] **Step 4: 통과 확인** — `./.venv/bin/pytest tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/app.py src/llmsearch/web/static/index.html tests/test_web.py
git commit -m "feat: 골든 평가 GUI — GET/PUT /api/eval/golden·run(자체 커넥션·50건·502 클래스명)·설정 탭 결과 표 (스펙 M7 §4)"
```

---

### Task 7: E2E 확장 + HANDOFF

**Files:**
- Modify: `tools/e2e/verify.py` (9.9 블록 끝 `재구축 후 배너 없음` 체크 뒤, `# 10.` 앞), `docs/HANDOFF.md`, `README.md`(골든 GUI 한 줄)

- [ ] **Step 1: 시나리오 삽입**

```python
    # 9.10 M7 — 채팅 필터(notes만) → 마지막 답변 카드 전부 notes + 필터 표시 줄 + 발췌 (스펙 M7 §2·§3)
    page.click("nav >> text=채팅")
    page.click("#filters summary")
    page.check(".srcChk[data-src='notes']")
    page.fill("#question", "프로젝트A 회고 개선점")
    before_cards = page.locator(".src").count()
    page.click("form >> text=검색")
    page.wait_for_function(f"document.querySelectorAll('.src').length > {before_cards}", timeout=10000)
    page.wait_for_timeout(300)
    last = page.locator(".msg-a").last
    kinds = [t.strip("()").split(" · ")[0] for t in last.locator(".src small").all_inner_texts()]
    check("필터: 카드 전부 notes", kinds and all(k == "notes" for k in kinds), str(kinds))
    check("필터 표시 줄", "필터(첫 검색 기준): 소스=notes" in page.locator(".filters-note").last.inner_text())
    snips = last.locator(".snip").all_inner_texts()
    check("출처 카드 발췌", len(snips) >= 1 and all(s.strip() for s in snips), str(snips[:1]))
    page.uncheck(".srcChk[data-src='notes']")  # 이후 단계(10단계 UI 채팅)에 영향 없게

    # 9.11 M7 — 골든 평가 GUI: 적중 1 + 확정 미스 1 → 50% (1/2) ❌ (스펙 M7 §4)
    page.click("nav >> text=설정")
    page.wait_for_selector("#goldenText")
    page.fill("#goldenText", "- question: 프로젝트A 킥오프 언제?\n  expect_source_id: kickoff.md\n"
                             "- question: 존재하지 않는 주제 XYZQW\n  expect_source_id: none.md\n")
    page.click("#saveGoldenBtn")
    page.wait_for_timeout(300)
    check("골든 저장 2건", "저장됨 · 2건" in page.locator("#goldenStatus").inner_text())
    dialogs.clear()
    page.click("#runGoldenBtn")  # confirm은 dialog 핸들러가 accept
    page.wait_for_selector("#goldenResult", state="visible", timeout=10000)
    header = page.locator("#goldenHeader").inner_text()
    check("골든 결과 헤더", "50% (1/2)" in header and "❌" in header, header)
    check("골든 결과 표 2행", page.locator("#goldenTable tbody tr").count() == 2)
    check("골든 미스 표시", "❌" in page.locator("#goldenTable tbody tr").nth(1).inner_text())
```

- [ ] **Step 2: 실행 검증** — 데모 서버 기동 → `./.venv/bin/python tools/e2e/verify.py` → `총 73건 전부 PASS` (66 + 7). 예산: 채팅 2 + 평가 임베딩 ≤2 → ≈34 < 50.

- [ ] **Step 3: HANDOFF·README** — HANDOFF §1 표에 `| M7 검색 품질·평가 | ✅ 머지 | 채팅 필터(선검색 강제·툴 기본값·Claude 고지), 툴 스키마 현행화, 출처 발췌, 골든 평가 GUI |`, 기준 테스트 수/E2E 73 갱신, §3 다음 작업 = M8(채팅 UX) 스펙, §5 문서 지도에 M7 스펙·계획, §6 수동 게이트 "M7: 실 Claude로 필터 질의 시 고지가 답변에 반영되는지, sender 필터로 메일만 나오는지, golden.yaml 실데이터 실행". README 골든 절에 "설정 탭에서도 편집·실행 가능" 한 줄.

- [ ] **Step 4: Commit**

```bash
git add tools/e2e/verify.py docs/HANDOFF.md README.md
git commit -m "test: E2E 확장 — M7 채팅 필터·발췌·골든 평가 GUI 시나리오 (전 항목 PASS)"
```

---

## M7 수동 체크리스트 (실환경 — 머지 후 사용자 확인)

1. 필터에서 outlook_mail + 발신자 입력 → 메일만 근거로 답변, 실 Claude가 고지를 받아 필요 시 다른 소스를 명시 검색하는지(로그의 툴 호출 인자)
2. 출처 카드 발췌가 실제 매칭 문장인지 확인
3. 실데이터 golden.yaml(10~20건) 설정 탭에서 실행 → 적중률 ≥ 70% (상위 스펙 §1 성공 기준)
