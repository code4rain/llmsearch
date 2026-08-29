# llmsearch M6b — rebuild 인덱스 재구축·스키마 불일치 복구 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스펙 M6 §6 — 제자리 인덱스 초기화 + local_docs 요약 md 재사용(LLM 미호출) 재인덱싱, 중단 재개 마커, 스키마 불일치 상태에서의 기동·배너·복구, CLI `--rebuild`, E2E.

**Architecture:** `rebuild.py`가 웹·CLI 공용 로직을 갖는다: 사전 검사(`precheck`) → 제자리 초기화(`reset_index`: documents 전 행 삭제·local_docs 외 sync_state 삭제·`meta.rebuild_in_progress` 마커, **para_map·local_docs 상태 보존, 파일 삭제 없음**) → 백그라운드 재수집(`start_resync`). local_docs는 1회성 `force_reindex` 플래그로 요약 md를 읽어 Document를 만들고(summarizer 미호출) 삭제 판정을 생략하며, 관측 못 한 sid는 센티널로 남긴다. 스키마 불일치는 `create_app`이 기동을 살린 채 `state["schema_mismatch"]`로 보관하고, 복구는 `db.read_legacy_maps`로 para_map·local_docs 상태를 건진 뒤 파일을 재생성한다(열린 커넥션 없음). 마커는 local_docs `run_sync`가 플래그를 실제로 소비한 뒤에만 지운다.

**Tech Stack:** Python 3.12, FastAPI, SQLite, 표준 라이브러리만, Playwright E2E

**Spec:** `docs/superpowers/specs/2026-08-29-llmsearch-m6-design.md` §6, §7, §8 (M6b), §9, §10

**Rulings (계획 시점):**
- 스펙 §6의 "`/api/sources`에 `rebuild_in_progress` 필드"는 행마다 중복되므로 **`GET /api/status`** (`schema_mismatch`, `rebuild_in_progress`, `rebuilding`, `resummarizing`)로 대체한다. `/api/sources`의 `schema_mismatch` 필드(M6a)는 유지.
- 스펙 §6 CLI의 "`SOURCES`를 `llmsearch/sources.py`로 이동"은 불필요 — `rebuild.py`는 대상 소스 목록을 인자로 받고 `web/app.py`를 import하지 않는다(YAGNI).
- 마커 삭제는 `run_sync` 안에서 수행한다(local_docs 성공 + 플래그 소비 직후). 스케줄러가 먼저 소비하는 경우까지 한 지점에서 처리된다.
- 재개 엔드포인트는 `POST /api/rebuild/resume`로 분리한다(초기화 없이 재수집 스레드만 시작).

## Global Constraints

- 요약 md·para_map·local_docs 동기화 상태·Atlassian 등록·usage.json·rules.md는 **재구축이 절대 삭제하지 않는다** (상위 스펙 §3 "요약 md는 비용 산출물")
- `force_reindex` 경로는 summarizer를 호출하지 않는다 — 검증 지표: 재구축 전후 usage `summary`·`vision` 불변, `embed`만 증가
- `force_reindex` 경로는 삭제 판정을 하지 않고(`deleted=[]`), 관측 못 한 prev sid는 `[0.0, 0]` 센티널로 남긴다
- 상한 도달·폴더 미존재(force 아님)·진행 중이면 DB를 건드리기 전에 409
- 마커 `meta.rebuild_in_progress`는 local_docs `run_sync`가 `ok=True`로 `force_reindex` 플래그를 소비한 뒤에만 삭제; 진행 중 판정은 인메모리 `state["rebuilding"]`
- 엔드포인트는 커넥션을 `state`에서 호출 시점에 조회(M6a 완료) — 스키마 불일치 복구가 `state["conn"]`/`["read_conn"]`을 교체한다
- 웹 테스트 `TestClient(app, base_url="http://127.0.0.1")`; 질의 임베딩 캐시 때문에 embed 카운트 단언 테스트는 유일한 질문 문자열
- Python 4칸 들여쓰기, 표준 라이브러리만, 기존 297 테스트 무변경 통과, 태스크마다 전체 green
- E2E 기존 55건 무변경, 신규 시나리오는 verify.py 9.8 블록 끝과 `# 10.` 사이 (예산: 현재 ≈20건 + 재구축 embed ≈8 < 50)
- 커밋 메시지 한국어, `feat:`/`test:` 접두사

---

### Task 1: local_docs `force_reindex` — 요약 md 재사용·삭제 판정 생략·미관측 sid 센티널

**Files:**
- Modify: `src/llmsearch/connectors/local_docs.py`
- Test: `tests/test_local_docs.py` (추가)

**Interfaces:**
- Produces: `local_docs.DRM_MARKER = "🔒 DRM/암호화로 내용 미인덱싱"` (DRM 폴백 본문과 판정이 같은 상수 사용); `sync_local_docs(..., force_reindex: bool = False)`. Task 3의 `run_sync`가 `force_reindex=state.get("force_reindex_local_docs", False)`로 호출한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_local_docs.py` 끝에 추가:

```python
class _NoCallSummarizer(FakeSummarizer):
    """force_reindex 재사용 경로는 summarizer를 호출하면 안 된다."""

    def summarize_and_classify(self, *args, **kwargs):
        raise AssertionError("summarizer가 호출됨 — 요약 md 재사용 실패")

    def describe_filename(self, filename):
        raise AssertionError("describe_filename 호출됨")

    def describe_images(self, title, images):
        raise AssertionError("describe_images 호출됨")


def _force(tmp_path, docs, state, prior, summarizer=None, folders=None):
    return local_docs.sync_local_docs(
        folders=folders if folders is not None else [docs], excludes=[], overrides=[],
        summarizer=summarizer or _NoCallSummarizer(), summaries_dir=tmp_path / "summaries",
        projects=["프로젝트A"], areas=[], glossary="", class_rules="",
        state=state, prior_map=prior, force_reindex=True,
    )


def test_force_reindex_reuses_summary_md_without_llm(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "킥오프.pptx").write_bytes(b"fake-pptx")
    first = run(tmp_path, docs)  # 정상 요약 1회 → 요약 md + para 정보
    d0 = first.documents[0]
    prior = {d0.source_id: (d0.extra["para_path"], d0.extra["summary_path"])}

    r = _force(tmp_path, docs, first.state, prior)
    assert len(r.documents) == 1
    d = r.documents[0]
    assert d.text == Path(d0.extra["summary_path"]).read_text(encoding="utf-8")  # md 본문 그대로
    assert d.extra == {"para_path": d0.extra["para_path"], "summary_path": d0.extra["summary_path"]}
    assert d.content_indexed is True and d.title == "킥오프.pptx" and d.url_or_path == d0.source_id
    assert r.deleted_ids == [] and r.state["files"][d0.source_id] == first.state["files"][d0.source_id]


def test_force_reindex_drm_marker_and_missing_md_fallback(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "drm.pptx").write_bytes(b"x")       # DRM 폴백 본문 (마커 포함)
    (docs / "일반.pptx").write_bytes(b"y")
    first = run(tmp_path, docs)
    by_sid = {d.source_id: d for d in first.documents}
    prior = {sid: (d.extra["para_path"], d.extra["summary_path"]) for sid, d in by_sid.items()}
    drm_sid = next(s for s in by_sid if s.endswith("drm.pptx"))
    normal_sid = next(s for s in by_sid if s.endswith("일반.pptx"))
    assert local_docs.DRM_MARKER in by_sid[drm_sid].text
    Path(prior[normal_sid][1]).unlink()  # 요약 md 소실 → 정상 요약 경로 폴백(LLM 호출)

    r = _force(tmp_path, docs, first.state, prior, summarizer=FakeSummarizer())
    out = {d.source_id: d for d in r.documents}
    assert out[drm_sid].content_indexed is False          # 마커로 DRM 판정
    assert Path(prior[normal_sid][1]).exists()            # 폴백이 md를 다시 생성
    assert "## 요약" in out[normal_sid].text


def test_force_reindex_skips_deletion_and_keeps_unseen_as_sentinel(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "a.pptx").write_bytes(b"a")
    first = run(tmp_path, docs)
    d0 = first.documents[0]
    prior = {d0.source_id: (d0.extra["para_path"], d0.extra["summary_path"])}

    r = _force(tmp_path, docs, first.state, prior, folders=[tmp_path / "unmounted"])  # 폴더 미마운트
    assert r.documents == [] and r.deleted_ids == []
    assert Path(d0.extra["summary_path"]).exists()        # 요약 md unlink 없음
    assert r.state["files"][d0.source_id] == [0.0, 0]     # 다음 정상 동기화가 재처리
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_local_docs.py -v -k force_reindex`
Expected: FAIL — `TypeError: unexpected keyword 'force_reindex'`, `AttributeError: DRM_MARKER`

- [ ] **Step 3: 구현**

`src/llmsearch/connectors/local_docs.py` — 상수(`RETRY_SENTINEL` 아래):

```python
DRM_MARKER = "🔒 DRM/암호화로 내용 미인덱싱"  # DRM 폴백 본문 표식 — 재사용 경로가 content_indexed 판정에 씀
```

DRM 폴백 본문의 `f"(🔒 DRM/암호화로 내용 미인덱싱 — 파일명 기반)\n\n"`를 `f"({DRM_MARKER} — 파일명 기반)\n\n"`로 교체.

재사용 헬퍼(`_cleanup` 아래):

```python
def _reuse_summary(path: Path, sid: str, st, prior: tuple[str, str] | None) -> Document | None:
    """rebuild 재인덱싱: 기존 요약 md 본문으로 Document를 만든다 — summarizer 미호출 (스펙 M6 §6).

    md가 없거나 읽히지 않으면 None → 호출자가 정상 요약 경로로 폴백한다. _place는 호출하지 않는다
    (원본 재복사 불필요; para_overrides 재평가도 생략 — 반영은 재요약 버튼의 역할).
    """
    if not prior:
        return None
    summary_path = Path(prior[1])
    try:
        body = summary_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return Document(
        source_type="local_docs", source_id=sid, title=path.name, text=body, url_or_path=sid,
        updated_at=datetime.fromtimestamp(st.st_mtime),
        content_indexed=DRM_MARKER not in body,
        extra={"para_path": prior[0], "summary_path": prior[1]},
    )
```

`sync_local_docs` 시그니처 끝에 `force_reindex: bool = False` 추가. 루프의 시그니처 비교 블록을 다음으로 교체:

```python
            sig = [st.st_mtime, st.st_size]
            if prev.get(sid) == sig and not force_reindex:
                seen[sid] = sig  # 변경 없음 — 이미 처리된 파일로 유지
                continue

            prior = prior_map.get(sid)
            if force_reindex and prev.get(sid) == sig:
                reused = _reuse_summary(path, sid, st, prior)
                if reused is not None:
                    documents.append(reused)
                    seen[sid] = sig
                    continue
                # 요약 md 소실/손상 → 정상 요약 경로로 폴백 (아래)
```

함수 끝의 삭제 판정을 교체:

```python
    if force_reindex:
        # 재구축은 복원이지 정리가 아니다 — 미마운트 폴더 상태에서 전 문서가 deleted로 판정되어
        # 요약 md가 unlink되는 사고를 막는다. 관측 못 한 sid는 센티널로 남겨 prior_map을 보존하고
        # 다음 정상 동기화가 재처리·삭제 판정을 담당하게 한다 (스펙 M6 §6).
        for sid in prev:
            seen.setdefault(sid, list(RETRY_SENTINEL))
        return SyncResult(documents=documents, deleted_ids=[], state={"files": seen})
    deleted = [sid for sid in prev if sid not in seen]
    for sid in deleted:
        _cleanup(prior_map.get(sid))
    return SyncResult(documents=documents, deleted_ids=deleted, state={"files": seen})
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_local_docs.py -v` → PASS, `./.venv/bin/pytest -q` → 297 + 3 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/connectors/local_docs.py tests/test_local_docs.py
git commit -m "feat: local_docs force_reindex — 요약 md 재사용(LLM 미호출)·삭제 판정 생략·미관측 sid 센티널 (스펙 M6 §6)"
```

---

### Task 2: `rebuild.py` 핵심 — 마커·사전 검사·제자리 초기화 + `db.read_legacy_maps` + `indexer.delete_all_documents`

**Files:**
- Create: `src/llmsearch/rebuild.py`
- Modify: `src/llmsearch/db.py`, `src/llmsearch/indexer.py`
- Test: `tests/test_rebuild.py` (신규), `tests/test_db.py` (추가)

**Interfaces:**
- Produces: `indexer.delete_all_documents(conn) -> int` (커밋 안 함); `db.read_legacy_maps(path) -> tuple[list[tuple[str, str, str]], dict]`; `rebuild.REBUILD_MARKER = "rebuild_in_progress"`, `rebuild.RebuildRefused(detail, missing_folders=())`, `rebuild.marker_present(conn) -> bool`, `rebuild.set_marker(conn)`, `rebuild.clear_marker(conn)` (둘 다 커밋 안 함), `rebuild.precheck(state, force=False) -> None`, `rebuild.reset_index(state) -> dict`. Task 3이 전부 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_db.py` 끝에 추가:

```python
def test_read_legacy_maps_ignores_schema_version(tmp_path):
    from llmsearch import db, indexer

    path = tmp_path / "index.db"
    conn = db.open_db(path)
    indexer.set_para_map(conn, "/a.pptx", "Projects/프로젝트A", "/s/a.md")
    indexer.set_sync_state(conn, "local_docs", {"files": {"/a.pptx": [1.0, 2]}})
    indexer.set_sync_state(conn, "notes", {"files": {}})
    conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    conn.commit(); conn.close()
    import pytest
    with pytest.raises(db.SchemaMismatchError):
        db.open_db(path)  # 버전 불일치는 여전히 기동을 막는다
    rows, state = db.read_legacy_maps(path)
    assert rows == [("/a.pptx", "Projects/프로젝트A", "/s/a.md")]
    assert state == {"files": {"/a.pptx": [1.0, 2]}}
    assert db.read_legacy_maps(tmp_path / "none.db") == ([], {})
    (tmp_path / "junk.db").write_bytes(b"not sqlite")
    assert db.read_legacy_maps(tmp_path / "junk.db") == ([], {})
```

`tests/test_rebuild.py` 신규:

```python
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llmsearch import db, indexer, rebuild
from llmsearch.config import Config
from llmsearch.connectors import local_docs
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.llm import FakeAnswerer
from llmsearch.summarize import FakeSummarizer
from llmsearch.web.app import create_app


def make_state(tmp_path: Path, monkeypatch, daily_limit: int = 0):
    """notes 1 + local_docs 2(pptx 스텁) 구성의 앱 state. TestClient는 필요한 테스트만 만든다."""
    monkeypatch.setattr(local_docs, "extract_text", lambda p: f"{p.stem} 본문. 프로젝트A 관련 내용 " * 10)
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "kick.md").write_text("# 프로젝트A 킥오프\n8월 1일", encoding="utf-8")
    watch = tmp_path / "watch"; watch.mkdir()
    (watch / "설계.pptx").write_bytes(b"x")
    (watch / "회의록.pptx").write_bytes(b"y")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], watch_folders=[watch],
                 projects=["프로젝트A"], daily_api_call_limit=daily_limit)
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    return app, app.state.llmsearch


def doc_count(conn, source=None) -> int:
    if source:
        return conn.execute("SELECT COUNT(*) FROM documents WHERE source_type=?", (source,)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]


def test_marker_roundtrip(tmp_path: Path, monkeypatch):
    _, state = make_state(tmp_path, monkeypatch)
    conn = state["conn"]
    assert rebuild.marker_present(conn) is False
    rebuild.set_marker(conn); conn.commit()
    assert rebuild.marker_present(conn) is True
    assert rebuild.marker_present(db.open_db(state["config"].db_path)) is True  # 커밋됨 — 다른 커넥션에서 보임
    rebuild.clear_marker(conn); conn.commit()
    assert rebuild.marker_present(conn) is False


def test_precheck_refusals(tmp_path: Path, monkeypatch):
    _, state = make_state(tmp_path, monkeypatch, daily_limit=1)
    state["usage"].record("embed", 5)
    with pytest.raises(rebuild.RebuildRefused, match="상한"):
        rebuild.precheck(state)
    state["usage"].daily_limit = 0
    state["config"].watch_folders.append(tmp_path / "unmounted")
    with pytest.raises(rebuild.RebuildRefused) as ei:
        rebuild.precheck(state)
    assert ei.value.missing_folders == [str(tmp_path / "unmounted")]
    rebuild.precheck(state, force=True)  # force면 폴더 경고 무시
    state["config"].watch_folders.pop()
    state["rebuilding"] = True
    with pytest.raises(rebuild.RebuildRefused, match="진행 중"):
        rebuild.precheck(state)
    state["rebuilding"] = False
    state["resummarizing"] = True
    with pytest.raises(rebuild.RebuildRefused, match="진행 중"):
        rebuild.precheck(state)


def test_reset_index_keeps_para_map_and_local_state(tmp_path: Path, monkeypatch):
    from llmsearch.web.app import run_sync

    _, state = make_state(tmp_path, monkeypatch)
    run_sync(state, "notes"); run_sync(state, "local_docs")
    conn = state["conn"]
    assert doc_count(conn) == 3
    local_state = indexer.get_sync_state(conn, "local_docs")
    sid = next(iter(local_state["files"]))
    para_before = indexer.get_para_map(conn, sid)
    summary_md = Path(para_before[1])
    assert summary_md.exists()

    info = rebuild.reset_index(state)
    assert info == {"documents_deleted": 3}
    assert doc_count(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    assert indexer.get_sync_state(conn, "notes") == {}                 # 다른 소스 상태 삭제
    assert indexer.get_sync_state(conn, "local_docs") == local_state   # local_docs 상태 보존
    assert indexer.get_para_map(conn, sid) == para_before              # para_map 보존
    assert summary_md.exists()                                          # 요약 md 보존
    assert rebuild.marker_present(conn) is True
    assert state["force_reindex_local_docs"] is True
    assert indexer.delete_all_documents(conn) == 0
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_rebuild.py tests/test_db.py -v -k "rebuild or legacy or marker or precheck or reset_index"`
Expected: FAIL — `ImportError: llmsearch.rebuild`, `AttributeError: read_legacy_maps`

- [ ] **Step 3: 구현**

`src/llmsearch/indexer.py` (`delete_documents` 아래):

```python
def delete_all_documents(conn: sqlite3.Connection) -> int:
    """documents 전 행을 fts5/vec 정합성 있게 삭제 — rebuild 제자리 초기화용. 커밋은 호출자가."""
    ids = [r[0] for r in conn.execute("SELECT id FROM documents").fetchall()]  # 스캔 중 삭제 방지: fetchall 후 순회
    for doc_id in ids:
        _delete_doc_rows(conn, doc_id)
    return len(ids)
```

`src/llmsearch/db.py` (`open_db` 아래):

```python
def read_legacy_maps(path: Path) -> tuple[list[tuple[str, str, str]], dict]:
    """스키마 버전 검사 없이 para_map 전체와 local_docs sync_state만 읽는다 (스펙 M6 §6).

    스키마 불일치(M9 임베딩 차원 변경 등)로 open_db가 거부하는 index.db에서 요약 md 매핑을
    회수하기 위한 진입점 — 두 테이블은 스키마 변경 대상이 아니다. 파일이 없거나 sqlite가
    아니거나 테이블이 없으면 빈 결과.
    """
    if not path.exists():
        return [], {}
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error:
        return [], {}
    try:
        try:
            rows = [tuple(r) for r in conn.execute(
                "SELECT source_id, para_path, summary_path FROM para_map ORDER BY source_id").fetchall()]
        except sqlite3.Error:
            rows = []
        try:
            row = conn.execute("SELECT state_json FROM sync_state WHERE source_type='local_docs'").fetchone()
            state = json.loads(row[0]) if row else {}
            if not isinstance(state, dict):
                state = {}
        except (sqlite3.Error, ValueError):
            state = {}
    finally:
        conn.close()
    return rows, state
```

`src/llmsearch/rebuild.py` 신규:

```python
"""인덱스 재구축 — 웹(/api/rebuild)·CLI(--rebuild) 공용 로직 (스펙 M6 §6).

인덱스는 소모품, 요약 md·para_map·local_docs 동기화 상태·Atlassian 등록·usage.json·rules.md는
보존한다. 정상 경로는 파일을 지우지 않고 documents 행만 제자리에서 지운다(커넥션 유지 → 경쟁 창·
WAL 삭제 실패·스냅샷 파일이 전부 불필요). local_docs는 1회성 force_reindex 플래그로 요약 md를
읽어 재인덱싱하며(summarizer 미호출), 마커 meta.rebuild_in_progress는 그 플래그가 실제로
소비된 뒤(run_sync 성공)에만 지워져 재수집 도중 프로세스가 죽어도 재기동 시 [재개]로 이어진다.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from . import db, indexer

logger = logging.getLogger(__name__)

REBUILD_MARKER = "rebuild_in_progress"


class RebuildRefused(Exception):
    """사전 검사 실패 — DB를 건드리기 전에 거부 (HTTP 409 / CLI 종료코드 2)."""

    def __init__(self, detail: str, missing_folders: Sequence[str] = ()):
        super().__init__(detail)
        self.detail = detail
        self.missing_folders = list(missing_folders)


def marker_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (REBUILD_MARKER,)).fetchone()
    return row is not None


def set_marker(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, '1')", (REBUILD_MARKER,))


def clear_marker(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM meta WHERE key=?", (REBUILD_MARKER,))


def precheck(state: dict, force: bool = False) -> None:
    """아무것도 바꾸기 전의 거부 조건 (스펙 M6 §6 0단계). 진행 중 판정은 인메모리 플래그만 본다."""
    if state.get("rebuilding") or state.get("resummarizing"):
        raise RebuildRefused("재구축 또는 재요약이 진행 중입니다 — 끝난 뒤 다시 시도하세요")
    if not state["usage"].indexing_allowed():
        raise RebuildRefused("일일 API 호출 상한 도달 — 상한이 초기화된 뒤 재구축하세요 "
                             "(초기화 후 게이트에 막히면 빈 인덱스로 자정까지 고착됩니다)")
    cfg = state["config"]
    missing = [str(p) for p in [*cfg.watch_folders, *cfg.notes_folders] if not Path(p).exists()]
    if missing and not force:
        raise RebuildRefused("감시/노트 폴더를 찾을 수 없습니다 — 드라이브 마운트를 확인하거나 "
                             "force로 건너뛰고 진행하세요: " + ", ".join(missing), missing)


def reset_index(state: dict) -> dict:
    """제자리 초기화 — documents 전 행·local_docs 외 sync_state 삭제 + 마커, 단일 트랜잭션."""
    with state["sync_lock"]:
        conn: sqlite3.Connection = state["conn"]
        deleted = indexer.delete_all_documents(conn)
        conn.execute("DELETE FROM sync_state WHERE source_type != 'local_docs'")
        set_marker(conn)
        conn.commit()
        state["force_reindex_local_docs"] = True
    logger.info("인덱스 초기화 — documents %d건 삭제, para_map·local_docs 상태 보존", deleted)
    return {"documents_deleted": deleted}
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_rebuild.py tests/test_db.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/rebuild.py src/llmsearch/db.py src/llmsearch/indexer.py tests/test_rebuild.py tests/test_db.py
git commit -m "feat: rebuild 핵심 — 마커·사전 검사·제자리 초기화, read_legacy_maps, delete_all_documents (스펙 M6 §6)"
```

---

### Task 3: 웹 통합 — run_sync 플래그·마커, 백그라운드 재수집, 스키마 불일치 기동·복구, `/api/status`·`/api/rebuild`·`/api/rebuild/resume`

**Files:**
- Modify: `src/llmsearch/rebuild.py`, `src/llmsearch/web/app.py`
- Test: `tests/test_rebuild.py` (추가)

**Interfaces:**
- Consumes: Task 1 `force_reindex`, Task 2 `precheck`/`reset_index`/마커/`read_legacy_maps`
- Produces: `rebuild.start_resync(state, run_sync, sources) -> threading.Thread` (`state["rebuilding"]`, `state["rebuild_thread"]`); `rebuild.recover_schema_mismatch(state) -> dict`; `create_app`이 `SchemaMismatchError`를 잡아 `state["schema_mismatch"]`(정상 시 None)로 보관하고 기동 시 마커가 있으면 `state["force_reindex_local_docs"]=True`; `run_sync`가 local_docs에 `force_reindex` 전달 + 성공 시 플래그 해제·마커 삭제; `scheduler_loop`는 `rebuilding` 중 라운드 스킵; `GET /api/status` → `{schema_mismatch, rebuild_in_progress, rebuilding, resummarizing}`; `POST /api/rebuild {force?}` → `{ok, phase:"resync", targets, documents_deleted|legacy_maps_recovered}` 또는 409 `{detail, missing_folders}`; `POST /api/rebuild/resume` → `{ok, phase, targets}`. Task 4 CLI와 Task 5 UI/E2E가 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_rebuild.py` 끝에 추가:

```python
def client_of(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def wait_resync(state, timeout=30):
    t = state.get("rebuild_thread")
    assert t is not None
    t.join(timeout)
    assert not t.is_alive() and state["rebuilding"] is False


def test_rebuild_endpoint_restores_docs_without_llm(tmp_path: Path, monkeypatch):
    app, state = make_state(tmp_path, monkeypatch)
    client = client_of(app)
    client.post("/api/sync/notes"); client.post("/api/sync/local_docs")
    usage_before = dict(state["usage"].today_by_kind())
    conn = state["read_conn"]
    assert doc_count(conn) == 3
    sid = next(iter(indexer.get_sync_state(conn, "local_docs")["files"]))
    para_before = indexer.get_para_map(conn, sid)

    r = client.post("/api/rebuild", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["phase"] == "resync" and body["documents_deleted"] == 3
    assert body["targets"] == ["notes", "local_docs", "outlook_mail", "outlook_cal"]  # 등록 없는 confluence/jira 제외
    wait_resync(state)

    after = state["usage"].today_by_kind()
    assert after.get("summary", 0) == usage_before.get("summary", 0)  # 요약 md 재사용 — LLM 미호출
    assert after.get("vision", 0) == usage_before.get("vision", 0)
    assert after["embed"] > usage_before["embed"]
    assert doc_count(conn, "notes") == 1 and doc_count(conn, "local_docs") == 2
    assert indexer.get_para_map(conn, sid) == para_before
    assert rebuild.marker_present(conn) is False                       # local_docs 성공 후 마커 삭제
    assert state["force_reindex_local_docs"] is False
    assert client.get("/api/status").json() == {
        "schema_mismatch": None, "rebuild_in_progress": False, "rebuilding": False, "resummarizing": False}
    log_sources = [e["source"] for e in state["log"][:4]]
    assert set(log_sources) >= {"notes", "local_docs"}


def test_rebuild_refusals_and_force(tmp_path: Path, monkeypatch):
    app, state = make_state(tmp_path, monkeypatch, daily_limit=1)
    client = client_of(app)
    client.post("/api/sync/notes")  # embed 1 → 상한 도달
    r = client.post("/api/rebuild", json={})
    assert r.status_code == 409 and "상한" in r.json()["detail"]
    assert doc_count(state["read_conn"]) == 1  # DB 무변경

    state["usage"].daily_limit = 0
    state["config"].watch_folders.append(tmp_path / "unmounted")
    r = client.post("/api/rebuild", json={})
    assert r.status_code == 409 and r.json()["missing_folders"] == [str(tmp_path / "unmounted")]
    r = client.post("/api/rebuild", json={"force": True})
    assert r.status_code == 200
    wait_resync(state)
    assert doc_count(state["read_conn"], "notes") == 1
    assert client.post("/api/rebuild", json={}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_marker_survives_until_local_docs_succeeds(tmp_path: Path, monkeypatch):
    """마커는 local_docs run_sync가 플래그를 소비한 뒤에만 삭제 — 게이트에 막히면 유지."""
    from llmsearch.web.app import run_sync

    _, state = make_state(tmp_path, monkeypatch)
    run_sync(state, "local_docs")
    rebuild.reset_index(state)
    state["usage"].daily_limit = 1  # 이미 초과
    entry = run_sync(state, "local_docs")
    assert entry["ok"] is False
    assert rebuild.marker_present(state["conn"]) is True and state["force_reindex_local_docs"] is True
    state["usage"].daily_limit = 0
    entry = run_sync(state, "local_docs")
    assert entry["ok"] is True and entry["indexed"] == 2
    assert rebuild.marker_present(state["conn"]) is False and state["force_reindex_local_docs"] is False


def test_startup_detects_marker_and_resume(tmp_path: Path, monkeypatch):
    app1, state1 = make_state(tmp_path, monkeypatch)
    from llmsearch.web.app import run_sync
    run_sync(state1, "local_docs")
    rebuild.reset_index(state1)  # 재수집 전에 프로세스가 죽었다고 가정
    state1["conn"].close(); state1["read_conn"].close()

    cfg = state1["config"]
    app2 = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                      answerer=FakeAnswerer(), enable_scheduler=False)
    state2 = app2.state.llmsearch
    assert state2["force_reindex_local_docs"] is True
    client = client_of(app2)
    assert client.get("/api/status").json()["rebuild_in_progress"] is True
    r = client.post("/api/rebuild/resume", json={})
    assert r.status_code == 200 and r.json()["phase"] == "resync"
    wait_resync(state2)
    assert doc_count(state2["read_conn"], "local_docs") == 2
    assert client.get("/api/status").json()["rebuild_in_progress"] is False
    assert client.post("/api/rebuild/resume", json={}).status_code == 409  # 재개할 것 없음


def test_schema_mismatch_boot_and_recover(tmp_path: Path, monkeypatch):
    app1, state1 = make_state(tmp_path, monkeypatch)
    from llmsearch.web.app import run_sync
    run_sync(state1, "notes"); run_sync(state1, "local_docs")
    usage_before = dict(state1["usage"].today_by_kind())
    sid = next(iter(indexer.get_sync_state(state1["conn"], "local_docs")["files"]))
    para_before = indexer.get_para_map(state1["conn"], sid)
    state1["conn"].execute("UPDATE meta SET value='0' WHERE key='schema_version'"); state1["conn"].commit()
    state1["conn"].close(); state1["read_conn"].close()

    cfg = state1["config"]
    app2 = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                      answerer=FakeAnswerer(), enable_scheduler=False)  # 기동 성공
    state2 = app2.state.llmsearch
    assert state2["conn"] is None and "schema" in state2["schema_mismatch"]
    client = client_of(app2)
    assert client.post("/api/chat", json={"question": "스키마 불일치 질의", "history": []}).status_code == 503
    assert client.post("/api/sync/notes").status_code == 503
    s = client.get("/api/status").json()
    assert s["schema_mismatch"] and s["rebuild_in_progress"] is False
    assert client.get("/api/sources").json()[0]["schema_mismatch"]

    r = client.post("/api/rebuild", json={})
    assert r.status_code == 200 and r.json()["legacy_maps_recovered"] == 2
    wait_resync(state2)
    conn = state2["read_conn"]
    assert conn is not None and state2["schema_mismatch"] is None
    assert doc_count(conn, "local_docs") == 2 and doc_count(conn, "notes") == 1
    assert indexer.get_para_map(conn, sid) == para_before
    after = state2["usage"].today_by_kind()
    assert after.get("summary", 0) == usage_before.get("summary", 0)  # legacy 매핑 회수 → 요약 재사용
    assert client.post("/api/sync/notes").status_code == 200           # 가드 해제
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_rebuild.py -v`
Expected: FAIL — `/api/rebuild` 404, `AttributeError: start_resync`, 스키마 불일치 테스트는 `create_app`이 `SchemaMismatchError`를 던짐

- [ ] **Step 3: 구현**

`src/llmsearch/rebuild.py`에 추가:

```python
def start_resync(state: dict, run_sync: Callable[[dict, str], dict], sources: Sequence[str]) -> threading.Thread:
    """백그라운드 재수집 — 수천 문서·메일 1년치는 수십 분이 걸리므로 HTTP 요청 안에서 기다리지 않는다.

    마커는 여기서 지우지 않는다 — local_docs run_sync가 force_reindex 플래그를 소비할 때 지운다.
    """
    if state.get("rebuilding"):
        raise RebuildRefused("재구축이 이미 진행 중입니다")
    state["rebuilding"] = True
    targets = list(sources)

    def target():
        try:
            for source in targets:
                entry = run_sync(state, source)
                logger.info("재수집 %s: ok=%s indexed=%s", source, entry["ok"], entry["indexed"])
        finally:
            state["rebuilding"] = False

    thread = threading.Thread(target=target, name="llmsearch-rebuild", daemon=True)
    state["rebuild_thread"] = thread
    thread.start()
    return thread


def recover_schema_mismatch(state: dict) -> dict:
    """스키마 불일치 상태의 재구축 — legacy 매핑 회수 → 파일 재생성 → 매핑 복원 → 커넥션 교체."""
    cfg = state["config"]
    with state["sync_lock"]:
        rows, local_state = db.read_legacy_maps(cfg.db_path)
        for suffix in ("", "-wal", "-shm"):
            Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)  # 열린 커넥션 없음(conn is None)
        conn = db.open_db(cfg.db_path)
        read_conn = db.open_db(cfg.db_path)
        for sid, para_path, summary_path in rows:
            conn.execute("INSERT OR REPLACE INTO para_map(source_id, para_path, summary_path) VALUES (?,?,?)",
                         (sid, para_path, summary_path))
        if local_state:
            indexer.set_sync_state(conn, "local_docs", local_state)
        set_marker(conn)
        conn.commit()
        state["conn"], state["read_conn"] = conn, read_conn
        state["schema_mismatch"] = None
        state["force_reindex_local_docs"] = True
    if not rows:
        logger.warning("legacy 매핑을 회수하지 못함 — local_docs 전량 재요약 (요약 API 소모)")
    return {"legacy_maps_recovered": len(rows), "documents_deleted": 0}
```

`src/llmsearch/web/app.py`:

import에 `from .. import db, indexer, rebuild, search`, `from fastapi.responses import FileResponse, JSONResponse, StreamingResponse`.

`run_sync` local_docs 분기 — `renderer=_get_slide_renderer(state),` 뒤에 `force_reindex=bool(state.get("force_reindex_local_docs")),` 추가. `indexer.set_sync_state(conn, source, result.state)` 직후:

```python
            if source == "local_docs" and state.get("force_reindex_local_docs"):
                # 플래그는 커넥터가 정상 반환한 뒤에만 소비 — 마커도 이 시점에만 삭제 (스펙 M6 §6)
                state["force_reindex_local_docs"] = False
                rebuild.clear_marker(conn)
                conn.commit()
```

`create_app` DB 오픈 블록 교체:

```python
    try:
        conn = db.open_db(config.db_path)
        # 쓰기는 conn(run_sync 전용), 읽기는 read_conn — 동기화 쓰기 트랜잭션 중에도
        # /api/chat, /api/sources 같은 읽기 요청이 같은 커넥션을 공유하지 않게 분리한다.
        read_conn = db.open_db(config.db_path)
        schema_mismatch = None
    except db.SchemaMismatchError as exc:
        # 기동은 살린다 — GUI 배너의 [재구축]으로 복구 (M9 임베딩 차원 변경이 이 경로를 탄다)
        conn = read_conn = None
        schema_mismatch = str(exc)
        _logger.error("index.db 스키마 불일치 — 재구축 필요: %s", exc)
```

`state` 리터럴에 `"schema_mismatch": schema_mismatch, "rebuilding": False, "force_reindex_local_docs": False,` 추가. 리터럴 직후:

```python
    if conn is not None and rebuild.marker_present(conn):
        state["force_reindex_local_docs"] = True  # 이전 재구축이 완료되지 않음 — 배너 [재개]
        _logger.warning("이전 재구축이 완료되지 않았습니다 — 설정 탭에서 [재개]하세요")
```

`scheduler_loop` for 루프 앞에 `if state.get("rebuilding"): continue` (sleep 뒤).

엔드포인트(`/api/usage` 뒤):

```python
    @app.get("/api/status")
    def status():
        conn = state["read_conn"]
        return {"schema_mismatch": state.get("schema_mismatch"),
                "rebuild_in_progress": conn is not None and rebuild.marker_present(conn),
                "rebuilding": bool(state.get("rebuilding")),
                "resummarizing": bool(state.get("resummarizing"))}

    @app.post("/api/rebuild", dependencies=[Depends(local_origin_only)])
    def rebuild_index(payload: dict):
        """인덱스 재구축 (스펙 M6 §6). 초기화·복원은 동기, 재수집은 백그라운드 — 진행은 소스 탭·로그 탭."""
        force = payload.get("force") is True
        try:
            rebuild.precheck(state, force=force)
            info = rebuild.recover_schema_mismatch(state) if state.get("schema_mismatch") else rebuild.reset_index(state)
            targets = _scheduled_sources(state)
            rebuild.start_resync(state, run_sync, targets)
        except rebuild.RebuildRefused as exc:
            return JSONResponse(status_code=409, content={"detail": exc.detail, "missing_folders": exc.missing_folders})
        return {"ok": True, "phase": "resync", "targets": targets, **info}

    @app.post("/api/rebuild/resume", dependencies=[Depends(local_origin_only)])
    def rebuild_resume():
        _require_db()
        if not rebuild.marker_present(state["read_conn"]):
            raise HTTPException(409, "재개할 재구축이 없습니다")
        try:
            targets = _scheduled_sources(state)
            rebuild.start_resync(state, run_sync, targets)
        except rebuild.RebuildRefused as exc:
            raise HTTPException(409, exc.detail)
        return {"ok": True, "phase": "resync", "targets": targets}
```

`/api/status`·`/api/rebuild`는 `_require_db()`를 걸지 않는다(스키마 불일치 상태에서 동작해야 함).

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_rebuild.py tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/rebuild.py src/llmsearch/web/app.py tests/test_rebuild.py
git commit -m "feat: rebuild 웹 통합 — 백그라운드 재수집·마커 소비·스키마 불일치 기동/복구·/api/status·/api/rebuild(/resume) (스펙 M6 §6)"
```

---

### Task 4: CLI `--rebuild [--yes]`

**Files:**
- Modify: `src/llmsearch/rebuild.py`, `src/llmsearch/__main__.py`
- Test: `tests/test_rebuild.py` (추가)

**Interfaces:**
- Produces: `rebuild.run_cli(state, run_sync, sources, yes=False, input_fn=input, out=print) -> int` (종료코드: 0 성공, 2 거부/취소); `python -m llmsearch --config C --rebuild [--yes]`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_run_cli_headless_rebuild(tmp_path: Path, monkeypatch):
    from llmsearch.web.app import _scheduled_sources, run_sync

    _, state = make_state(tmp_path, monkeypatch)
    run_sync(state, "notes"); run_sync(state, "local_docs")
    summary_before = state["usage"].today_by_kind().get("summary", 0)
    lines: list[str] = []
    code = rebuild.run_cli(state, run_sync, _scheduled_sources(state), yes=True, out=lines.append)
    assert code == 0
    assert doc_count(state["read_conn"]) == 3 and rebuild.marker_present(state["conn"]) is False
    assert state["usage"].today_by_kind().get("summary", 0) == summary_before
    assert any("documents 3" in ln or "3건" in ln for ln in lines) and any("local_docs" in ln for ln in lines)


def test_run_cli_prompt_and_refusal(tmp_path: Path, monkeypatch):
    from llmsearch.web.app import _scheduled_sources, run_sync

    _, state = make_state(tmp_path, monkeypatch)
    run_sync(state, "notes")
    lines: list[str] = []
    code = rebuild.run_cli(state, run_sync, _scheduled_sources(state), yes=False,
                           input_fn=lambda _p: "n", out=lines.append)
    assert code == 2 and doc_count(state["read_conn"]) == 1  # 취소 — 무변경
    state["usage"].daily_limit = 1; state["usage"].record("embed", 5)
    code = rebuild.run_cli(state, run_sync, _scheduled_sources(state), yes=True, out=lines.append)
    assert code == 2 and any("상한" in ln for ln in lines)


def test_main_parses_rebuild_flags(monkeypatch, tmp_path: Path):
    import llmsearch.__main__ as m

    calls = {}
    monkeypatch.setattr(m, "load_config", lambda p: Config(data_dir=tmp_path / "data"))
    monkeypatch.setattr(m, "create_app", lambda cfg: type("A", (), {"state": type("S", (), {"llmsearch": {"x": 1}})()})())
    monkeypatch.setattr(m, "run_cli", lambda state, run_sync, sources, yes: calls.update(yes=yes, state=state) or 0)
    monkeypatch.setattr(m, "_scheduled_sources", lambda state: ["notes"])
    monkeypatch.setattr(m.uvicorn, "run", lambda *a, **k: calls.update(served=True))
    monkeypatch.setattr("sys.argv", ["llmsearch", "--config", "c.yaml", "--rebuild", "--yes"])
    m.main()
    assert calls == {"yes": True, "state": {"x": 1}, "served": True}
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_rebuild.py -v -k cli`
Expected: FAIL — `AttributeError: run_cli`, `argparse` unrecognized `--rebuild`

- [ ] **Step 3: 구현**

`src/llmsearch/rebuild.py`에 추가:

```python
def run_cli(state: dict, run_sync: Callable[[dict, str], dict], sources: Sequence[str],
            yes: bool = False, input_fn: Callable[[str], str] = input,
            out: Callable[[str], None] = print) -> int:
    """헤드리스 재구축 — 서버 기동 전에 동기로 초기화·재수집. 0=성공, 2=거부/취소."""
    conn = state.get("conn")
    if conn is None:
        out(f"index.db 스키마 불일치: {state.get('schema_mismatch')} — legacy 매핑을 회수해 재구축합니다")
    else:
        n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        out(f"현재 인덱스 documents {n}건 — 전부 지우고 {', '.join(sources)} 순서로 재수집합니다 "
            "(local_docs는 요약 md 재사용, 변경된 파일만 요약 API 호출)")
    out("주의: 재구축 1회로 일일 API 상한을 초과할 수 있습니다 (게이트는 소스 진입 시점만 검사)")
    if not yes and input_fn("계속할까요? [y/N] ").strip().lower() not in ("y", "yes"):
        out("취소됨")
        return 2
    try:
        precheck(state)
        info = recover_schema_mismatch(state) if state.get("schema_mismatch") else reset_index(state)
    except RebuildRefused as exc:
        out(f"재구축 거부: {exc.detail}")
        return 2
    out(f"초기화 완료: {info}")
    for source in sources:
        entry = run_sync(state, source)
        out(f"재수집 {source}: ok={entry['ok']} indexed={entry['indexed']}"
            + (f" error={entry['error'].splitlines()[0]}" if entry["error"] else ""))
    return 0
```

`src/llmsearch/__main__.py`:

```python
import argparse
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .config import load_config
from .rebuild import run_cli
from .web.app import _scheduled_sources, create_app, run_sync


def main():
    parser = argparse.ArgumentParser(prog="llmsearch")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--rebuild", action="store_true", help="기동 전 인덱스 재구축 (요약 md 재사용)")
    parser.add_argument("--yes", action="store_true", help="--rebuild 확인 프롬프트 생략")
    args = parser.parse_args()
    load_dotenv()
    app = create_app(load_config(args.config))
    if args.rebuild:
        state = app.state.llmsearch
        code = run_cli(state, run_sync, _scheduled_sources(state), yes=args.yes)
        if code != 0:
            sys.exit(code)
    uvicorn.run(app, host="127.0.0.1", port=args.port)  # 로컬 전용 (스펙 §10)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_rebuild.py -v` → PASS, `./.venv/bin/pytest -q` → 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/rebuild.py src/llmsearch/__main__.py tests/test_rebuild.py
git commit -m "feat: CLI --rebuild [--yes] — 헤드리스 재구축 후 기동 (스펙 M6 §6)"
```

---

### Task 5: UI 배너·재구축 버튼 + E2E + HANDOFF

**Files:**
- Modify: `src/llmsearch/web/static/index.html`, `tools/e2e/verify.py`, `docs/HANDOFF.md`, `README.md`
- Test: `tests/test_web.py` (추가 1건)

**Interfaces:**
- Consumes: Task 3 `/api/status`, `/api/rebuild`, `/api/rebuild/resume`
- Produces: `#banner`(상단), `#rebuildBtn`(설정 탭 운영), JS `loadStatus()`, `rebuildIndex()`, `resumeRebuild()`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py` 끝에 추가:

```python
def test_banner_and_rebuild_button_in_index(tmp_path: Path):
    client = make_app(tmp_path)
    html = client.get("/").text
    assert 'id="banner"' in html and 'id="rebuildBtn"' in html and "loadStatus()" in html
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v -k banner` → FAIL

- [ ] **Step 3: 구현**

`index.html` — `<h1>llmsearch</h1>` 아래에 `<div id="banner" style="display:none;background:#fff3cd;border:1px solid #e0c060;padding:.5rem;margin:.5rem 0"></div>`. 설정 탭 운영 섹션의 전체 재요약 버튼 뒤에 `<button id="rebuildBtn" onclick="rebuildIndex()">인덱스 재구축</button>`. `loadSources()` 첫 줄 `loadUsage();` 뒤에 `loadStatus();`, 그리고 `<script>` 끝에 페이지 로드 시 `loadStatus();` 호출 추가. JS:

```js
async function loadStatus() {
  const s = await (await fetch('/api/status')).json();
  const b = document.getElementById('banner');
  b.replaceChildren();
  let text = '', action = null;
  if (s.schema_mismatch) {
    text = 'index.db 스키마 불일치 — 재구축이 필요합니다 (요약 md는 재사용): ' + s.schema_mismatch;
    action = ['재구축', rebuildIndex];
  } else if (s.rebuilding) {
    text = '재구축 진행 중 — 소스 탭 문서 수·로그 탭을 참조하세요 (메일은 백로그로 이어집니다)';
  } else if (s.rebuild_in_progress) {
    text = '이전 재구축이 완료되지 않았습니다';
    action = ['재개', resumeRebuild];
  }
  if (!text) { b.style.display = 'none'; return; }
  b.append(document.createTextNode(text + ' '));
  if (action) { const btn = document.createElement('button'); btn.textContent = action[0]; btn.onclick = action[1]; b.append(btn); }
  b.style.display = 'block';
}
async function postRebuild(body) {
  const r = await fetch('/api/rebuild', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)});
  return [r, await r.json()];
}
async function rebuildIndex() {
  const srcs = await (await fetch('/api/sources')).json();
  const total = srcs.reduce((a, s) => a + (s.doc_count || 0), 0);
  if (!confirm(`인덱스를 초기화하고 전 소스를 다시 수집합니다 (현재 문서 ${total}건). 요약 md는 재사용되어 ` +
               `변경된 파일만 요약 API를 호출하지만, 임베딩은 전량 다시 호출되어 일일 상한을 초과할 수 있습니다. 계속할까요?`)) return;
  let [r, d] = await postRebuild({});
  if (r.status === 409 && d.missing_folders && d.missing_folders.length) {
    if (!confirm(`폴더를 찾을 수 없습니다:\n${d.missing_folders.join('\n')}\n\n건너뛰고 진행할까요? (해당 문서는 다음 동기화에서 재처리)`)) return;
    [r, d] = await postRebuild({force: true});
  }
  alert(r.ok ? `재구축 시작 — 재수집 대상: ${d.targets.join(', ')}` : (d.detail || '재구축 실패'));
  loadStatus(); loadSources();
}
async function resumeRebuild() {
  const r = await fetch('/api/rebuild/resume', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const d = await r.json();
  alert(r.ok ? `재수집 재개 — 대상: ${d.targets.join(', ')}` : (d.detail || '재개 실패'));
  loadStatus();
}
```

`README.md` 비용 통제 절 아래에 "## 인덱스 재구축" 절: 설정 탭 [인덱스 재구축] 또는 `python -m llmsearch --config config.yaml --rebuild [--yes]`; 요약 md 재사용, 재수집은 백그라운드, 중단 시 배너 [재개], 스키마 불일치 시 배너 [재구축].

- [ ] **Step 4: E2E 시나리오 삽입**

`tools/e2e/verify.py` — 9.8 블록의 마지막 `check("전체 재요약: summary +1", ...)` 뒤, `# 10.` 앞에:

```python
    # 9.9 M6b — 인덱스 재구축: 요약 md 재사용(summary/vision 불변), 문서 수 복원 (스펙 M6 §6)
    before = usage_today()
    counts_before = {r["source"]: r["doc_count"] for r in page.request.get(f"{BASE}/api/sources").json()}
    dialogs.clear()
    page.click("nav >> text=설정")
    page.click("#rebuildBtn")  # confirm·alert는 dialog 핸들러가 accept
    page.wait_for_timeout(500)
    check("재구축 시작 alert", any("재구축 시작" in m for m in dialogs), " / ".join(m[:40] for m in dialogs))
    page.wait_for_function(
        "fetch('/api/status').then(r => r.json()).then(s => !s.rebuilding && !s.rebuild_in_progress)",
        timeout=30000, polling=500)
    counts_after = {r["source"]: r["doc_count"] for r in page.request.get(f"{BASE}/api/sources").json()}
    for src in ("notes", "local_docs", "outlook_mail", "outlook_cal"):
        check(f"재구축 후 문서 수 복원: {src}", counts_after[src] == counts_before[src],
              f"{counts_before[src]}→{counts_after[src]}")
    after = usage_today()
    check("재구축: summary 불변(요약 md 재사용)", after.get("summary", 0) == before.get("summary", 0))
    check("재구축: vision 불변", after.get("vision", 0) == before.get("vision", 0))
    check("재구축: embed 증가", after.get("embed", 0) > before.get("embed", 0))
    page.click("nav >> text=소스")
    page.wait_for_timeout(300)
    check("재구축 후 배너 없음", page.locator("#banner").is_hidden())
```

Run: 데모 서버 기동 → `./.venv/bin/python tools/e2e/verify.py` → `총 64건 전부 PASS` (55 + 9). 예산: 9.9 전 ≈20건 + embed ≈8(notes 2 + rules.md 1 + local 1 + mail 1 + cal 1 + confluence 2) ≈ 28 < 50.

- [ ] **Step 5: HANDOFF·커밋**

`docs/HANDOFF.md`: §1 표에 `| M6b rebuild | ✅ 머지 | 제자리 초기화·요약 md 재사용·마커 재개·스키마 불일치 배너/복구·CLI --rebuild |`, 기준 테스트 수/E2E 64 갱신, §3 다음 작업 = M7(검색 품질·평가) 스펙 작성, §5 문서 지도에 m6b 계획, §6 수동 게이트에 "M6b: 실 데이터로 [인덱스 재구축] 1회 — summary/vision 카운트 불변 확인, `--rebuild --yes` 헤드리스 확인".

```bash
git add src/llmsearch/web/static/index.html tools/e2e/verify.py docs/HANDOFF.md README.md tests/test_web.py
git commit -m "test: E2E 확장 — M6b 인덱스 재구축 시나리오 + 배너·재구축 UI·README (전 항목 PASS)"
```

---

## M6b 수동 체크리스트 (실환경 — 머지 후 사용자 확인)

1. 설정 탭 [인덱스 재구축] → 배너 "재구축 진행 중" → 완료 후 소스 문서 수 복원, usage.json의 summary/vision 불변·embed 증가
2. 재수집 도중 앱 종료 → 재기동 시 배너 "[재개]" → 재개 후 완료
3. `python -m llmsearch --config config.yaml --rebuild --yes` 헤드리스 재구축 후 정상 기동
