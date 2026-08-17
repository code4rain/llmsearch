# llmsearch M4 — 잔여 P1 마무리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스펙 잔여 P1 3건 구현 — ① 이미지 위주 PPT 슬라이드 비전 보완, ② Atlassian 서비스별 자격증명 분리, ③ Archive 워크플로(GUI 프로젝트 완료 처리).

**Architecture:** ① 렌더러는 `SlideRenderer` Protocol로 분리(Windows PowerPoint COM 구현 + Fake), 비전 설명은 `Summarizer.describe_images`로 확장 — WSL 테스트는 Fake, 실렌더링은 M2 `check_outlook` 패턴의 수동 게이트. ② `resolve_auth_candidates`에 service 프리픽스(CONFLUENCE_/JIRA_) 우선 + ATLASSIAN_ 폴백을 넣고, 웹 계층은 서비스별 클라이언트(`confluence_client`/`jira_client`)를 독립 lazy 생성·독립 401 리셋. ③ `archive.py`가 `summaries/Projects/<name>/` → `Archives/<name>/` 폴더 이동과 documents/para_map 경로 갱신을 원자적으로(실패 시 이동 롤백) 수행 — 검색의 `Archives/` 감쇠는 para_path 프리픽스를 보므로 즉시 적용된다.

**Tech Stack:** Python 3.12, FastAPI, sqlite3, httpx, google-genai(비전), pywin32(PowerPoint COM — Windows 전용, 지연 import)

**Spec:** `docs/superpowers/specs/2026-08-17-llmsearch-design.md` (§7.1 항목 4·7, §7.2 M3 구현 노트의 자격증명 제약)

## Global Constraints

- 자격증명·API 키는 `.env` 환경변수로만 — config.yaml·코드·로그·예외 메시지·repr에 평문 금지
- 테스트는 실 네트워크·실 COM·실 브라우저 호출 금지 — Fake/monkeypatch/MockTransport만
- 웹은 127.0.0.1 + TrustedHostMiddleware 유지, UI 동적 값은 `esc()` 이스케이프
- Python 들여쓰기 4칸 (전역 CLAUDE.md의 탭 규칙은 GDScript 전용)
- 전체 스위트 `./.venv/bin/pytest` — 시작 기준 218개, 태스크마다 전부 green 유지
- 커밋 메시지는 기존 스타일(한국어, `feat:`/`fix:`/`docs:` 접두사)
- 파일 단위 실패는 소스 동기화 전체를 중단시키지 않는다 (로그 + 건너뜀 격리 유지)
- 비전 보완 실패는 항상 기존 경로(텍스트만/DRM 폴백)로 소리 없이 강등 — 새 기능이 기존 인덱싱을 깨면 안 됨

---

### Task 1: SlideRenderer Protocol + Fake + Summarizer.describe_images

**Files:**
- Create: `src/llmsearch/render.py`
- Modify: `src/llmsearch/summarize.py` (Protocol·Fake·Gemini에 `describe_images` 추가)
- Test: `tests/test_render.py`, `tests/test_summarize.py` (추가)

**Interfaces:**
- Consumes: `GeminiSummarizer.client`(google-genai Client), `self.model` — 기존 필드 그대로
- Produces: `SlideRenderer` Protocol — `render(path: Path, max_slides: int = MAX_SLIDES) -> list[bytes]` (PNG 바이트 목록); `FakeSlideRenderer(images: dict[str, list[bytes]])` (파일명 키); `Summarizer.describe_images(title: str, images: list[bytes]) -> str`. Task 2가 이 시그니처를 그대로 사용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_render.py` 신규:

```python
from pathlib import Path

from llmsearch.render import MAX_SLIDES, FakeSlideRenderer


def test_fake_renderer_returns_registered_images():
    fake = FakeSlideRenderer(images={"deck.pptx": [b"png1", b"png2"]})
    out = fake.render(Path("/any/deck.pptx"))
    assert out == [b"png1", b"png2"]
    assert fake.calls == ["/any/deck.pptx"]


def test_fake_renderer_caps_at_max_slides():
    fake = FakeSlideRenderer(images={"big.pptx": [b"x"] * (MAX_SLIDES + 5)})
    assert len(fake.render(Path("big.pptx"))) == MAX_SLIDES


def test_fake_renderer_unknown_file_returns_empty():
    assert FakeSlideRenderer().render(Path("none.pptx")) == []
```

`tests/test_summarize.py`에 추가:

```python
def test_fake_describe_images_is_deterministic():
    from llmsearch.summarize import FakeSummarizer

    out = FakeSummarizer().describe_images("발표.pptx", [b"a", b"b", b"c"])
    assert "3" in out and "발표.pptx" in out
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_render.py tests/test_summarize.py -v`
Expected: FAIL — `ModuleNotFoundError: llmsearch.render`, `AttributeError: describe_images`

- [ ] **Step 3: 구현**

`src/llmsearch/render.py` 신규:

```python
"""슬라이드 → PNG 렌더러 (스펙 §7.1 P1 — 이미지 위주 PPT 비전 보완).

렌더링은 Windows PowerPoint COM 전용이라 Protocol로 분리한다 — WSL 테스트는 Fake,
실동작은 scripts/check_ppt_render.py 수동 게이트로 검증한다 (M2 check_outlook 패턴).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Protocol

MAX_SLIDES = 10  # 비전 API 비용·지연 상한 — 핵심 내용은 앞쪽 슬라이드에 있다는 가정


class SlideRenderer(Protocol):
    def render(self, path: Path, max_slides: int = MAX_SLIDES) -> list[bytes]: ...


class FakeSlideRenderer:
    """테스트용 — 파일명 키로 등록된 PNG 바이트를 돌려준다."""

    def __init__(self, images: dict[str, list[bytes]] | None = None):
        self.images = images or {}
        self.calls: list[str] = []

    def render(self, path: Path, max_slides: int = MAX_SLIDES) -> list[bytes]:
        self.calls.append(str(path))
        return self.images.get(Path(path).name, [])[:max_slides]


class PowerPointRenderer:
    """PowerPoint COM으로 슬라이드를 PNG 내보내기 — ComWorker STA 스레드에서 실행.

    WSL/리눅스에서는 생성만 가능하고 render()는 win32com import에서 실패한다 —
    호출부(_augment_with_vision)가 예외를 격리하므로 동기화는 계속된다.
    PowerPoint 프로세스는 사용자가 띄워둔 세션일 수 있어 Quit()하지 않는다.
    """

    def __init__(self, worker):
        self.worker = worker

    def render(self, path: Path, max_slides: int = MAX_SLIDES) -> list[bytes]:
        return self.worker.submit(self._render_in_worker, Path(path).resolve(), max_slides)

    @staticmethod
    def _render_in_worker(path: Path, max_slides: int) -> list[bytes]:
        import win32com.client  # Windows 전용 — 지연 import

        app = win32com.client.Dispatch("PowerPoint.Application")
        app.DisplayAlerts = 1  # ppAlertsNone — 복구·암호 프롬프트 등 모달 억제. 모달이 뜨면
        # ComWorker.submit의 done.wait()가 타임아웃 없이 영구 블록되고, 이 워커를 Outlook과
        # 공유하므로 메일/일정 동기화까지 함께 동결된다 — 반드시 Open 전에 설정할 것.
        pres = app.Presentations.Open(str(path), ReadOnly=True, WithWindow=False)
        out: list[bytes] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                count = min(int(pres.Slides.Count), max_slides)
                for i in range(1, count + 1):  # COM 컬렉션은 1-기반 인덱싱이 방탄
                    png = Path(tmp) / f"slide{i}.png"
                    pres.Slides(i).Export(str(png), "PNG")
                    out.append(png.read_bytes())
        finally:
            pres.Close()
        return out
```

`src/llmsearch/summarize.py` 수정 — `Summarizer` Protocol에 메서드 추가:

```python
class Summarizer(Protocol):
    def summarize_and_classify(
        self, title: str, text: str, projects: list[str], areas: list[str],
        existing_resources: list[str], prior_category: str | None, glossary: str, rules: str,
    ) -> SummaryResult: ...

    def describe_filename(self, filename: str) -> str: ...

    def describe_images(self, title: str, images: list[bytes]) -> str: ...
```

`FakeSummarizer`에 추가:

```python
    def describe_images(self, title: str, images: list[bytes]) -> str:
        return f"슬라이드 {len(images)}장 비전 설명: {title}"
```

`GeminiSummarizer`에 추가:

```python
    def describe_images(self, title: str, images: list[bytes]) -> str:
        from google.genai import types  # 지연 import

        parts = [types.Part.from_bytes(data=img, mime_type="image/png") for img in images]
        prompt = (
            f"다음은 사내 발표자료 '{title}'의 슬라이드 이미지다. 각 슬라이드의 핵심 내용을 "
            "검색 가능한 텍스트로 설명하라. 수치·날짜·고유명사(사람/프로젝트명)를 보존하고, "
            "슬라이드별 불릿 1~3개로 요약하라."
        )
        try:
            resp = self.client.models.generate_content(model=self.model, contents=parts + [prompt])
            return resp.text or ""
        except Exception:
            return ""
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_render.py tests/test_summarize.py -v` → PASS, 이어서 `./.venv/bin/pytest -q` 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/render.py src/llmsearch/summarize.py tests/test_render.py tests/test_summarize.py
git commit -m "feat: SlideRenderer Protocol+Fake, Summarizer.describe_images — PPT 비전 보완 기반"
```

---

### Task 2: local_docs 비전 보완 통합

**Files:**
- Modify: `src/llmsearch/connectors/local_docs.py`
- Test: `tests/test_local_docs.py` (추가)

**Interfaces:**
- Consumes: Task 1의 `SlideRenderer.render`, `Summarizer.describe_images`
- Produces: `sync_local_docs(..., renderer: SlideRenderer | None = None)` — 마지막 키워드 인자 추가 (기본 None = 기존 동작 그대로). `VISION_MIN_CHARS = 200`, `_augment_with_vision(path, text, renderer, summarizer) -> str`. Task 3의 웹 배선이 `renderer=` 인자를 사용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_local_docs.py`에 추가 (기존 테스트 픽스처 스타일에 맞춰 — 이 파일의 기존 헬퍼로 pptx 자리에 더미 파일을 쓰고 `extract_text`를 monkeypatch하는 관례를 따른다):

```python
def test_vision_augments_short_pptx_text(tmp_path, monkeypatch):
    """이미지 위주 PPT: 추출 텍스트가 짧으면 렌더+비전 설명을 덧붙여 정상 인덱싱한다."""
    from llmsearch.connectors import local_docs
    from llmsearch.render import FakeSlideRenderer
    from llmsearch.summarize import FakeSummarizer

    src = tmp_path / "docs"
    src.mkdir()
    ppt = src / "image_deck.pptx"
    ppt.write_bytes(b"fake")
    # 72자 — garbled 임계(MIN_TEXT_LEN=50)는 확실히 넘고 비전 임계(200)는 확실히 못 미침
    monkeypatch.setattr(local_docs, "extract_text", lambda p: "표지 제목 텍스트 " * 8)
    renderer = FakeSlideRenderer(images={"image_deck.pptx": [b"p1", b"p2"]})

    r = local_docs.sync_local_docs(
        folders=[src], excludes=[], overrides=[], summarizer=FakeSummarizer(),
        summaries_dir=tmp_path / "sum", projects=[], areas=[], glossary="", class_rules="",
        state={}, prior_map={}, renderer=renderer,
    )
    assert len(r.documents) == 1
    d = r.documents[0]
    assert d.content_indexed is True
    assert "슬라이드 2장 비전 설명" in d.text  # FakeSummarizer.describe_images 결과가 요약 입력에 반영
    assert renderer.calls  # 렌더러가 실제로 호출됨


def test_vision_skipped_when_text_long_enough(tmp_path, monkeypatch):
    from llmsearch.connectors import local_docs
    from llmsearch.render import FakeSlideRenderer
    from llmsearch.summarize import FakeSummarizer

    src = tmp_path / "docs"
    src.mkdir()
    (src / "text_deck.pptx").write_bytes(b"fake")
    monkeypatch.setattr(local_docs, "extract_text", lambda p: "충분히 긴 본문 " * 50)
    renderer = FakeSlideRenderer(images={"text_deck.pptx": [b"p1"]})

    r = local_docs.sync_local_docs(
        folders=[src], excludes=[], overrides=[], summarizer=FakeSummarizer(),
        summaries_dir=tmp_path / "sum", projects=[], areas=[], glossary="", class_rules="",
        state={}, prior_map={}, renderer=renderer,
    )
    assert r.documents[0].content_indexed is True
    assert renderer.calls == []  # 임계치 이상이면 렌더링 자체를 안 함


def test_vision_failure_falls_back_to_existing_path(tmp_path, monkeypatch):
    """렌더러가 죽어도 기존 경로(짧은 텍스트 → DRM 폴백)로 강등되고 동기화는 계속된다."""
    from llmsearch.connectors import local_docs
    from llmsearch.summarize import FakeSummarizer

    class BoomRenderer:
        def render(self, path, max_slides=10):
            raise RuntimeError("COM dead")

    src = tmp_path / "docs"
    src.mkdir()
    (src / "broken.pptx").write_bytes(b"fake")
    monkeypatch.setattr(local_docs, "extract_text", lambda p: "짧음")

    r = local_docs.sync_local_docs(
        folders=[src], excludes=[], overrides=[], summarizer=FakeSummarizer(),
        summaries_dir=tmp_path / "sum", projects=[], areas=[], glossary="", class_rules="",
        state={}, prior_map={}, renderer=BoomRenderer(),
    )
    assert len(r.documents) == 1
    assert r.documents[0].content_indexed is False  # 기존 DRM 폴백 경로


def test_vision_not_used_for_non_pptx(tmp_path, monkeypatch):
    from llmsearch.connectors import local_docs
    from llmsearch.render import FakeSlideRenderer
    from llmsearch.summarize import FakeSummarizer

    src = tmp_path / "docs"
    src.mkdir()
    (src / "short.docx").write_bytes(b"fake")
    monkeypatch.setattr(local_docs, "extract_text", lambda p: "짧음")
    renderer = FakeSlideRenderer(images={"short.docx": [b"p1"]})

    local_docs.sync_local_docs(
        folders=[src], excludes=[], overrides=[], summarizer=FakeSummarizer(),
        summaries_dir=tmp_path / "sum", projects=[], areas=[], glossary="", class_rules="",
        state={}, prior_map={}, renderer=renderer,
    )
    assert renderer.calls == []
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_local_docs.py -v -k vision`
Expected: FAIL — `TypeError: sync_local_docs() got an unexpected keyword argument 'renderer'`

- [ ] **Step 3: 구현**

`src/llmsearch/connectors/local_docs.py` 수정:

상단 import·상수 추가:

```python
from ..render import SlideRenderer

VISION_MIN_CHARS = 200  # pptx 추출 텍스트가 이 미만이면 이미지 위주로 보고 비전 보완 (스펙 §7.1 P1)
```

헬퍼 추가 (`extract_text` 아래):

```python
def _augment_with_vision(path: Path, text: str, renderer: SlideRenderer | None,
                         summarizer: Summarizer) -> str:
    """이미지 위주 pptx의 짧은 추출 텍스트에 슬라이드 비전 설명을 덧붙인다 (스펙 §7.1 P1).

    실패는 어떤 경우에도 전파하지 않는다 — 비전 보완은 부가 기능이므로, 렌더러/비전 API가
    죽으면 원래 텍스트 그대로 기존 경로(garbled 판정 → DRM 폴백)를 타게 둔다.
    """
    if renderer is None or path.suffix.lower() != ".pptx":
        return text
    if len(text.strip()) >= VISION_MIN_CHARS:
        return text
    try:
        images = renderer.render(path)
        if not images:
            return text
        desc = summarizer.describe_images(path.name, images)
        if desc.strip():
            return text + "\n\n## 슬라이드 비전 설명\n" + desc.strip()
    except Exception:
        logger.warning("슬라이드 비전 보완 실패, 텍스트만 사용: %s", path, exc_info=True)
    return text
```

`sync_local_docs` 시그니처에 키워드 추가:

```python
def sync_local_docs(
    folders: list[Path], excludes: list[str], overrides: list[dict],
    summarizer: Summarizer, summaries_dir: Path,
    projects: list[str], areas: list[str], glossary: str, class_rules: str,
    state: dict, prior_map: dict[str, tuple[str, str]],
    renderer: SlideRenderer | None = None,
) -> SyncResult:
```

본문의 추출 블록을 다음으로 교체 (기존 `text = extract_text(path)` / `looks_garbled` 부분):

```python
                try:
                    text = extract_text(path)
                    text = _augment_with_vision(path, text, renderer, summarizer)
                    if looks_garbled(text):
                        raise ValueError("garbled")
                except Exception:
                    content_indexed = False
                    text = ""
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_local_docs.py -v` → PASS (기존 테스트 포함), `./.venv/bin/pytest -q` 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/connectors/local_docs.py tests/test_local_docs.py
git commit -m "feat: 이미지 위주 PPT 비전 보완 — 짧은 추출 텍스트에 슬라이드 설명 증강 (스펙 §7.1 P1)"
```

---

### Task 3: 웹 배선 + Windows 수동 게이트 스크립트

**Files:**
- Modify: `src/llmsearch/web/app.py` (`_get_slide_renderer`, `create_app(slide_renderer=...)`, run_sync local_docs 분기)
- Create: `scripts/check_ppt_render.py`
- Test: `tests/test_web.py` (추가)

**Interfaces:**
- Consumes: Task 2의 `sync_local_docs(..., renderer=)`, Task 1의 `PowerPointRenderer`, 기존 `ComWorker`
- Produces: `create_app(..., slide_renderer=None)` 주입 인자; `_get_slide_renderer(state)` — Windows(`hasattr(os, "startfile")`)에서만 PowerPointRenderer 지연 생성, 그 외 None

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py`에 추가 (이 파일의 기존 `create_app` 픽스처 관례를 따른다):

```python
def test_injected_slide_renderer_reaches_local_docs(tmp_path, monkeypatch):
    """create_app에 주입한 렌더러가 local_docs 동기화까지 전달된다."""
    from llmsearch.config import Config
    from llmsearch.connectors import local_docs
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.render import FakeSlideRenderer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app, run_sync

    docs = tmp_path / "watch"
    docs.mkdir()
    (docs / "deck.pptx").write_bytes(b"fake")
    # 72자 — garbled 임계(50) 초과·비전 임계(200) 미만: 증강 후에도 정상 인덱싱 경로 유지
    monkeypatch.setattr(local_docs, "extract_text", lambda p: "표지 제목 텍스트 " * 8)
    renderer = FakeSlideRenderer(images={"deck.pptx": [b"p1"]})
    cfg = Config(data_dir=tmp_path / "data", watch_folders=[docs])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), slide_renderer=renderer, enable_scheduler=False)
    entry = run_sync(app.state.llmsearch, "local_docs")
    assert entry["ok"] is True and entry["indexed"] == 1
    assert renderer.calls  # 주입된 렌더러가 실제 사용됨
    row = app.state.llmsearch["read_conn"].execute(
        "SELECT content_indexed FROM documents").fetchone()
    assert row[0] == 1  # 비전 증강 텍스트가 정상 인덱싱됨 (DRM 폴백 아님)


def test_slide_renderer_lazy_is_none_off_windows(tmp_path):
    """비Windows에서 지연 생성은 None — 비전 보완이 조용히 생략된다."""
    import os

    from llmsearch.web.app import _get_slide_renderer

    if hasattr(os, "startfile"):  # Windows에서는 이 테스트를 건너뜀 (COM 생성 방지)
        import pytest
        pytest.skip("non-Windows 전용 테스트")
    state = {}
    assert _get_slide_renderer(state) is None
    assert _get_slide_renderer(state) is None  # 캐시 후에도 동일
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v -k slide`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'slide_renderer'`

- [ ] **Step 3: 구현**

`src/llmsearch/web/app.py` 수정:

`_get_outlook_client` 아래 함수 추가:

```python
def _get_slide_renderer(state):
    """Windows에서만 PowerPoint COM 렌더러를 지연 생성 — 그 외 환경은 None(비전 보완 생략).

    ComWorker는 Outlook 클라이언트와 공유한다(STA 스레드 1개로 직렬화). 아직 없으면
    여기서 만들어 state["outlook_worker"]에 둔다 — 이후 _get_outlook_client도 재사용.
    """
    if "slide_renderer" not in state:
        import os

        if hasattr(os, "startfile"):
            from ..outlook.com_worker import ComWorker
            from ..render import PowerPointRenderer

            worker = state.get("outlook_worker")
            if worker is None:
                worker = ComWorker()
                state["outlook_worker"] = worker
            state["slide_renderer"] = PowerPointRenderer(worker)
        else:
            state["slide_renderer"] = None
    return state["slide_renderer"]
```

주의: `_get_outlook_client`도 같은 공유 원칙을 따르도록 수정한다 — 기존 무조건 `ComWorker()` 생성 부분을 `state.get("outlook_worker")` 재사용으로 교체:

```python
def _get_outlook_client(state):
    """실 클라이언트 지연 생성 — 테스트는 create_app 주입으로 이 경로를 타지 않는다."""
    if state.get("outlook_client") is None:
        from ..outlook.com_client import ThreadedOutlookClient
        from ..outlook.com_worker import ComWorker

        worker = state.get("outlook_worker")
        if worker is None:
            worker = ComWorker()
            state["outlook_worker"] = worker
        state["outlook_client"] = ThreadedOutlookClient(worker)
    return state["outlook_client"]
```

`run_sync`의 local_docs 분기에 인자 추가:

```python
                result = sync_local_docs(
                    folders=cfg.watch_folders, excludes=cfg.exclude, overrides=cfg.para_overrides,
                    summarizer=state["summarizer"], summaries_dir=cfg.summaries_dir,
                    projects=cfg.projects, areas=cfg.areas,
                    glossary=rules_md.get("용어집", ""), class_rules=rules_md.get("분류 규칙", ""),
                    state=prev, prior_map=prior_map,
                    renderer=_get_slide_renderer(state),
                )
```

`create_app` 시그니처·state 수정:

```python
def create_app(config: Config, embedder=None, summarizer=None, answerer=None,
               outlook_client=None, atlassian_client=None, slide_renderer=None,
               enable_scheduler: bool = True) -> FastAPI:
```

state 딕셔너리 구성 직후에 추가 (주입 시에만 키를 만들어 lazy 경로를 보존):

```python
    if slide_renderer is not None:
        state["slide_renderer"] = slide_renderer
```

`scripts/check_ppt_render.py` 신규:

```python
"""Windows 수동 게이트: PowerPoint COM 슬라이드 렌더링 실동작 확인 (M4).

사용: python scripts/check_ppt_render.py <pptx 경로>  (check_outlook과 동일하게 editable install 전제)
확인 항목: PowerPoint COM 기동, DisplayAlerts 억제 상태에서 WithWindow=False 열기,
Slide.Export PNG, 바이트 회수. POWERPNT.EXE는 Quit하지 않으므로 실행 후 상주한다 —
사용자 PowerPoint 세션을 죽이지 않기 위한 의도된 트레이드오프.
"""
import sys
from pathlib import Path

from llmsearch.outlook.com_worker import ComWorker
from llmsearch.render import PowerPointRenderer

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def main() -> None:
    if len(sys.argv) != 2:
        print("사용법: python scripts/check_ppt_render.py <pptx 경로>")
        sys.exit(1)
    worker = ComWorker()
    try:
        images = PowerPointRenderer(worker).render(Path(sys.argv[1]))
        print(f"렌더링 성공: 슬라이드 {len(images)}장")
        for i, img in enumerate(images):
            ok = "OK" if img[:8] == PNG_MAGIC else "FAIL"
            print(f"  slide{i}: {len(img)} bytes, PNG 시그니처={ok}")
    finally:
        worker.shutdown()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/app.py scripts/check_ppt_render.py tests/test_web.py
git commit -m "feat: 비전 렌더러 웹 배선 — Windows 지연 생성 + ComWorker 공유, check_ppt_render 수동 게이트"
```

---

### Task 4: 서비스별 자격증명 해석 (auth)

**Files:**
- Modify: `src/llmsearch/atlassian/auth.py`
- Test: `tests/test_atlassian_auth.py` (추가)

**Interfaces:**
- Consumes: 기존 `AtlassianAuth`
- Produces: `resolve_auth_candidates(env: Mapping | None = None, service: str | None = None)` — service는 `"confluence"` | `"jira"` | None. 서비스 지정 시 `CONFLUENCE_*`/`JIRA_*` 후보(PAT→Basic→쿠키 순)가 먼저, `ATLASSIAN_*` 후보가 그 뒤에 온다. None이면 기존과 동일(`ATLASSIAN_*`만). Task 5가 `service=` 인자를 사용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_atlassian_auth.py`에 추가:

```python
def test_service_specific_candidates_come_first():
    from llmsearch.atlassian.auth import resolve_auth_candidates

    env = {"CONFLUENCE_PAT": "cpat", "ATLASSIAN_PAT": "apat"}
    out = resolve_auth_candidates(env, service="confluence")
    assert [(a.mode, a.token) for a in out] == [("pat", "cpat"), ("pat", "apat")]


def test_service_falls_back_to_generic_when_no_specific():
    from llmsearch.atlassian.auth import resolve_auth_candidates

    env = {"ATLASSIAN_USER": "u", "ATLASSIAN_PASSWORD": "p"}
    out = resolve_auth_candidates(env, service="jira")
    assert len(out) == 1 and out[0].mode == "basic" and out[0].user == "u"


def test_jira_prefix_not_used_for_confluence():
    from llmsearch.atlassian.auth import resolve_auth_candidates

    env = {"JIRA_PAT": "jpat"}
    assert resolve_auth_candidates(env, service="confluence") == []
    assert [a.token for a in resolve_auth_candidates(env, service="jira")] == ["jpat"]


def test_no_service_keeps_legacy_behavior():
    from llmsearch.atlassian.auth import resolve_auth_candidates

    env = {"CONFLUENCE_PAT": "cpat", "ATLASSIAN_COOKIE": "ck"}
    out = resolve_auth_candidates(env)
    assert [a.mode for a in out] == ["cookie"]  # 서비스 프리픽스는 service 지정 시에만
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_atlassian_auth.py -v -k service or legacy`
Expected: FAIL — `TypeError: resolve_auth_candidates() got an unexpected keyword argument 'service'`

- [ ] **Step 3: 구현**

`src/llmsearch/atlassian/auth.py` 수정 — `_HELP`와 `resolve_auth_candidates` 교체:

```python
_HELP = (
    "Atlassian 인증 실패 — .env에 다음 중 하나를 설정하세요: "
    "ATLASSIAN_PAT(권장) / ATLASSIAN_USER+ATLASSIAN_PASSWORD / ATLASSIAN_COOKIE(브라우저 세션). "
    "Confluence와 Jira의 자격증명이 다르면 CONFLUENCE_*/JIRA_* 프리픽스로 서비스별 설정 가능"
)

_SERVICE_PREFIXES = {"confluence": "CONFLUENCE", "jira": "JIRA"}


def _candidates_for_prefix(e: Mapping[str, str], prefix: str) -> list[AtlassianAuth]:
    out: list[AtlassianAuth] = []
    if e.get(f"{prefix}_PAT"):
        out.append(AtlassianAuth(mode="pat", token=e[f"{prefix}_PAT"]))
    if e.get(f"{prefix}_USER") and e.get(f"{prefix}_PASSWORD"):
        out.append(AtlassianAuth(mode="basic", user=e[f"{prefix}_USER"],
                                 password=e[f"{prefix}_PASSWORD"]))
    if e.get(f"{prefix}_COOKIE"):
        out.append(AtlassianAuth(mode="cookie", cookie=e[f"{prefix}_COOKIE"]))
    return out


def resolve_auth_candidates(env: Mapping[str, str] | None = None,
                            service: str | None = None) -> list[AtlassianAuth]:
    """3단 폴백 후보 목록 (PAT → Basic → 쿠키, 스펙 §7.2 P0).

    service("confluence"|"jira")를 주면 서비스 전용 프리픽스(CONFLUENCE_/JIRA_) 후보가
    먼저 오고 공용 ATLASSIAN_ 후보가 폴백으로 뒤따른다 — DC의 PAT·세션 쿠키는 인스턴스별
    발급이라 두 서버의 자격증명이 다를 수 있기 때문 (M3 파킹 결정의 해소).
    """
    e = os.environ if env is None else env
    out: list[AtlassianAuth] = []
    if service is not None:
        out.extend(_candidates_for_prefix(e, _SERVICE_PREFIXES[service]))
    out.extend(_candidates_for_prefix(e, "ATLASSIAN"))
    return out
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_atlassian_auth.py -v` → PASS (기존 테스트 포함), `./.venv/bin/pytest -q` 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/atlassian/auth.py tests/test_atlassian_auth.py
git commit -m "feat: 서비스별 자격증명 프리픽스 — CONFLUENCE_*/JIRA_* 우선, ATLASSIAN_* 공용 폴백"
```

---

### Task 5: 웹 계층 서비스별 클라이언트 분리

**Files:**
- Modify: `src/llmsearch/web/app.py`, `.env.example`, `README.md`
- Test: `tests/test_web_atlassian.py` (기존 테스트 기대값 수정 + 추가)

**Interfaces:**
- Consumes: Task 4의 `resolve_auth_candidates(service=)`, 기존 `HttpAtlassianClient(confluence_base, jira_base, auth)`, `diagnose`
- Produces: state 키 `"confluence_client"` / `"jira_client"` (기존 단일 `"atlassian_client"` 키 폐지); `_get_atlassian_client(state, service)` — service별 base URL만 가진 클라이언트를 독립 진단·캐시; `create_app(atlassian_client=...)` 주입은 두 키 모두에 같은 인스턴스를 넣는다(기존 테스트·데모 호환). 401 리셋은 실패한 source의 키만 리셋.

**기존 테스트 기대값 변경 (특성화 테스트 규칙 — 동작 의도가 바뀌므로 기대값을 실제 새 동작에 맞춘다):**
- `test_confluence_401_resets_client_and_guides_reauth`: `state["atlassian_client"] is None` → `state["confluence_client"] is None`, 그리고 `state["jira_client"] is not None` (다른 서비스는 영향 없음) 단언 추가. 마지막 exact-match 단언의 기대 문자열은 아래 새 `_AUTH_EXPIRED_MSG` 전문으로 교체 (부분 문자열 단언 "인증이 만료되었습니다"/"다시 동기화"는 그대로 유효)
- `test_jira_401_resets_client_and_guides_reauth`: 대칭으로 `state["jira_client"] is None` + `state["confluence_client"] is not None`
- `test_auth_failure_isolated`: monkeypatch로 `ATLASSIAN_*` 4종에 더해 `CONFLUENCE_PAT/USER/PASSWORD/COOKIE`, `JIRA_PAT/USER/PASSWORD/COOKIE`도 `delenv(..., raising=False)`로 제거 (실환경 변수 누출 방지)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web_atlassian.py`에 추가 + 위 기대값 수정:

```python
def test_per_service_env_reaches_diagnose(tmp_path, monkeypatch):
    """confluence 동기화는 service='confluence' 후보로 진단한다 — JIRA_ 전용 자격증명은 배제."""
    from llmsearch.web.app import _get_atlassian_client

    for var in ("ATLASSIAN_PAT", "ATLASSIAN_USER", "ATLASSIAN_PASSWORD", "ATLASSIAN_COOKIE",
                "CONFLUENCE_PAT", "CONFLUENCE_USER", "CONFLUENCE_PASSWORD", "CONFLUENCE_COOKIE",
                "JIRA_PAT", "JIRA_USER", "JIRA_PASSWORD", "JIRA_COOKIE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JIRA_PAT", "jpat")  # confluence에는 후보가 없음

    from llmsearch.config import Config

    cfg = Config(data_dir=tmp_path, confluence_base_url="https://wiki.example.com",
                 jira_base_url="https://jira.example.com")
    state = {"config": cfg}
    try:
        _get_atlassian_client(state, "confluence")
        assert False, "후보가 없으니 RuntimeError가 나야 함"
    except RuntimeError as exc:
        assert "ATLASSIAN_" in str(exc)  # 안내 메시지 (자격증명 값은 미포함)


def test_injected_client_serves_both_services(tmp_path):
    """create_app(atlassian_client=...)는 confluence/jira 모두에 주입 인스턴스를 쓴다."""
    client = make_client(tmp_path, atlassian=fake_atlassian())  # 이 파일의 기존 헬퍼 재사용
    state = client.app.state.llmsearch
    assert state["confluence_client"] is not None  # None is None 동어반복 방지
    assert state["confluence_client"] is state["jira_client"]
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_web_atlassian.py -v`
Expected: FAIL — `_get_atlassian_client() takes 1 positional argument`, KeyError `confluence_client`

- [ ] **Step 3: 구현**

`src/llmsearch/web/app.py` 수정:

`_get_atlassian_client` 교체:

```python
def _get_atlassian_client(state, service: str):
    """서비스별 3단 폴백 진단으로 클라이언트 지연 생성 (스펙 §7.2 P0). 서비스별 세션 캐시.

    Confluence/Jira는 자격증명(PAT·쿠키)이 인스턴스별일 수 있어 클라이언트를 서비스별로
    분리한다 — 진단·401 리셋도 서비스 단위로 독립이다. 자격증명 부재는 diagnose()가
    안내 메시지와 함께 RuntimeError로 알리고, base URL 미설정은 자격증명이 있을 때만
    더 구체적으로 안내한다 (둘 다 빈 로컬 개발 환경에서 자격증명 안내가 먼저 보이도록).
    """
    key = f"{service}_client"
    if state.get(key) is None:
        cfg = state["config"]
        base = cfg.confluence_base_url if service == "confluence" else cfg.jira_base_url
        candidates = resolve_auth_candidates(service=service)
        if candidates and not base:
            raise RuntimeError(
                f"config.yaml의 atlassian.{service}_base_url을 설정하세요"
            )
        from ..atlassian.http_client import HttpAtlassianClient

        def factory(auth):
            if service == "confluence":
                return HttpAtlassianClient(base, "", auth)
            return HttpAtlassianClient("", base, auth)

        client, _auth = diagnose(candidates, factory)
        state[key] = client
    return state[key]
```

`run_sync`의 confluence/jira 분기와 401 처리 수정:

```python
            elif source == "confluence":
                client = _get_atlassian_client(state, "confluence")
                result = sync_confluence(client, state["registry"].confluence_page_ids(),
                                         prev, cfg.data_dir / "confluence")
            else:  # jira
                client = _get_atlassian_client(state, "jira")
                result = sync_jira(client, state["registry"].jira_keys(),
                                   prev, cfg.data_dir / "jira")
```

`_AUTH_EXPIRED_MSG` 상수를 서비스별 변수 안내로 교체 (기존 401 테스트 2건의 exact-match 기대 문자열도 이 전문으로 갱신):

```python
_AUTH_EXPIRED_MSG = (
    "Atlassian 인증이 만료되었습니다. .env의 자격증명(ATLASSIAN_* 또는 서비스별 "
    "CONFLUENCE_*/JIRA_*)을 갱신한 뒤 다시 동기화하세요."
)
```

`httpx.HTTPStatusError` except 절 전체를 다음으로 교체 (else 분기 유실 금지 — 비-Atlassian 401도 error 필드를 채워야 한다):

```python
        except httpx.HTTPStatusError as exc:
            conn.rollback()  # 실패한 트랜잭션의 부분 반영 방지 — 다음 동기화가 깨끗한 상태에서 시작
            entry["ok"] = False
            if exc.response.status_code == 401 and source in ("confluence", "jira"):
                # 실패한 서비스의 클라이언트만 리셋 — 다음 동기화 때 diagnose()가 다시 돈다
                # (스펙 §7.2 P0, 앱 재시작 없이 복구). 다른 서비스 세션은 그대로 유지.
                state[f"{source}_client"] = None
                entry["error"] = _AUTH_EXPIRED_MSG
                _logger.warning("Atlassian 401 — %s 클라이언트 캐시 리셋: %s", source, _AUTH_EXPIRED_MSG)
            else:
                entry["error"] = f"{exc}\n{traceback.format_exc(limit=3)}"
```

`create_app`의 state 구성에서 `"atlassian_client": atlassian_client` 항목을 다음 두 항목으로 교체:

```python
             "confluence_client": atlassian_client,
             "jira_client": atlassian_client,
```

`.env.example`에 추가 (기존 ATLASSIAN_* 블록 아래):

```bash
# 서비스별 자격증명이 다를 때만 설정 — 있으면 공용 ATLASSIAN_*보다 우선 (PAT·쿠키는 서버별 발급)
CONFLUENCE_PAT=
JIRA_PAT=
```

`README.md`의 Atlassian 섹션에서 `- **제약**: 자격증명은 서비스별로 분리되지 않고 하나의 세트를`으로 시작하는 불릿 문단(4줄, "통하지 않을 수 있다."까지)을 다음으로 교체:

```markdown
- **자격증명 설정**: 공용 `ATLASSIAN_*` 변수로 두 서버를 함께 인증하거나, 서버별로 다르면
  `CONFLUENCE_*` / `JIRA_*` 프리픽스로 각각 설정한다 (서비스 전용 변수가 공용보다 우선).
  DC의 PAT·세션쿠키는 인스턴스별 발급이므로 병용 시 서비스별 설정 또는 Basic(사내 AD 계정)
  모드를 권장한다.
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_web_atlassian.py -v` → PASS, `./.venv/bin/pytest -q` 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/app.py tests/test_web_atlassian.py .env.example README.md
git commit -m "feat: Atlassian 서비스별 클라이언트 분리 — 독립 진단·401 리셋, 서비스별 env 우선"
```

---

### Task 6: archive_project — 폴더 이동 + 인덱스 갱신

**Files:**
- Create: `src/llmsearch/archive.py`
- Test: `tests/test_archive.py`

**Interfaces:**
- Consumes: `summarize._sanitize_segment`, sqlite 스키마 `documents(para_path, extra_json)`, `para_map(source_id, para_path, summary_path)`
- Produces: `archive_project(conn, summaries_dir: Path, name: str) -> dict` — 반환 `{"project", "documents", "mappings", "hint"}`. 검증 실패는 `ValueError`(이름 불량/대상 존재), `KeyError`(폴더 없음). Task 7의 API가 이 함수와 예외 계약을 사용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_archive.py` 신규:

```python
import json
from pathlib import Path

import pytest

from llmsearch import db, indexer
from llmsearch.archive import archive_project


def _setup(tmp_path: Path):
    conn = db.open_db(tmp_path / "index.db")
    summaries = tmp_path / "summaries"
    proj = summaries / "Projects" / "알파"
    proj.mkdir(parents=True)
    summary = proj / "보고서.pptx.md"
    summary.write_text("# 요약", encoding="utf-8")
    (proj / "보고서.pptx").write_bytes(b"orig")
    conn.execute(
        "INSERT INTO documents(source_type, source_id, title, url_or_path, updated_at,"
        " content_indexed, para_path, extra_json) VALUES (?,?,?,?,?,?,?,?)",
        ("local_docs", "C:\\docs\\보고서.pptx", "보고서.pptx", "C:\\docs\\보고서.pptx",
         "2026-08-01T00:00:00", 1, "Projects/알파",
         json.dumps({"para_path": "Projects/알파", "summary_path": str(summary)},
                    ensure_ascii=False)),
    )
    indexer.set_para_map(conn, "C:\\docs\\보고서.pptx", "Projects/알파", str(summary))
    conn.commit()
    return conn, summaries


def test_archive_moves_folder_and_updates_index(tmp_path: Path):
    conn, summaries = _setup(tmp_path)
    out = archive_project(conn, summaries, "알파")
    assert out["documents"] == 1 and out["mappings"] == 1
    assert not (summaries / "Projects" / "알파").exists()
    new_summary = summaries / "Archives" / "알파" / "보고서.pptx.md"
    assert new_summary.exists() and (summaries / "Archives" / "알파" / "보고서.pptx").exists()
    row = conn.execute("SELECT para_path, extra_json FROM documents").fetchone()
    assert row[0] == "Archives/알파"
    extra = json.loads(row[1])
    assert extra["para_path"] == "Archives/알파" and extra["summary_path"] == str(new_summary)
    pm = indexer.get_para_map(conn, "C:\\docs\\보고서.pptx")
    assert pm == ("Archives/알파", str(new_summary))
    assert "config.yaml" in out["hint"] and "알파" in out["hint"]


def test_archive_unknown_project_raises(tmp_path: Path):
    conn, summaries = _setup(tmp_path)
    with pytest.raises(KeyError):
        archive_project(conn, summaries, "없음")


def test_archive_rejects_bad_name(tmp_path: Path):
    conn, summaries = _setup(tmp_path)
    with pytest.raises(ValueError):
        archive_project(conn, summaries, "..")
    with pytest.raises(ValueError):
        archive_project(conn, summaries, "a/b")


def test_archive_target_exists_raises(tmp_path: Path):
    conn, summaries = _setup(tmp_path)
    (summaries / "Archives" / "알파").mkdir(parents=True)
    with pytest.raises(ValueError):
        archive_project(conn, summaries, "알파")
    assert (summaries / "Projects" / "알파").exists()  # 원본 그대로


def test_archive_rolls_back_move_on_db_failure(tmp_path: Path, monkeypatch):
    """SQL 갱신이 실패하면 폴더 이동을 되돌린다 — 파일/인덱스 불일치 방지."""
    conn, summaries = _setup(tmp_path)
    conn.close()  # 닫힌 커넥션 → UPDATE에서 ProgrammingError
    import sqlite3

    with pytest.raises(sqlite3.ProgrammingError):
        archive_project(conn, summaries, "알파")
    assert (summaries / "Projects" / "알파").exists()
    assert not (summaries / "Archives" / "알파").exists()
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: llmsearch.archive`

- [ ] **Step 3: 구현**

`src/llmsearch/archive.py` 신규:

```python
"""프로젝트 완료(Archive) 워크플로 (스펙 §7.1 P1).

GUI에서 프로젝트 완료 처리 → `summaries/Projects/<name>/` 폴더를 `Archives/<name>/`로
이동하고, documents(para_path, extra_json)와 para_map을 새 경로로 갱신한다. 검색 랭킹의
Archives/ 감쇠(스펙 §8)는 para_path 프리픽스를 보므로 이 갱신만으로 즉시 적용된다.
원본 파일(watch 폴더)은 건드리지 않는다 — 다음 local_docs 동기화는 para_map의 사전
분류(prior)가 Archives/<name>이므로 재분류 없이 그대로 유지된다.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

# summarize의 내부 헬퍼를 의도적으로 재사용 — 프로젝트 이름이 파일시스템 세그먼트로
# 안전한지(경로 구분자·`..`·예약명 없음)를 분류 경로와 같은 규칙으로 판정하기 위해서다.
from .summarize import _sanitize_segment


def archive_project(conn: sqlite3.Connection, summaries_dir: Path, name: str) -> dict:
    if not name or _sanitize_segment(name) != name:
        raise ValueError(f"잘못된 프로젝트 이름입니다: {name!r}")
    src = summaries_dir / "Projects" / name
    dst = summaries_dir / "Archives" / name
    if not src.is_dir():
        raise KeyError(f"Projects/{name} 폴더가 없습니다")
    if dst.exists():
        raise ValueError(f"Archives/{name}가 이미 있습니다 — 기존 폴더를 정리한 뒤 다시 시도하세요")

    old_para, new_para = f"Projects/{name}", f"Archives/{name}"
    old_prefix, new_prefix = str(src), str(dst)

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    try:
        moved_docs = 0
        for doc_id, extra_json in conn.execute(
            "SELECT id, extra_json FROM documents WHERE para_path=?", (old_para,)
        ).fetchall():
            extra = json.loads(extra_json or "{}")
            extra["para_path"] = new_para
            sp = extra.get("summary_path")
            if isinstance(sp, str) and sp.startswith(old_prefix):
                extra["summary_path"] = new_prefix + sp[len(old_prefix):]
            conn.execute(
                "UPDATE documents SET para_path=?, extra_json=? WHERE id=?",
                (new_para, json.dumps(extra, ensure_ascii=False), doc_id),
            )
            moved_docs += 1

        moved_maps = 0
        for source_id, summary_path in conn.execute(
            "SELECT source_id, summary_path FROM para_map WHERE para_path=?", (old_para,)
        ).fetchall():
            new_summary = (
                new_prefix + summary_path[len(old_prefix):]
                if summary_path.startswith(old_prefix) else summary_path
            )
            conn.execute(
                "UPDATE para_map SET para_path=?, summary_path=? WHERE source_id=?",
                (new_para, new_summary, source_id),
            )
            moved_maps += 1
        conn.commit()
    except Exception:
        # DB 갱신 실패 시 폴더 이동을 되돌린다 — 파일과 인덱스가 서로 다른 위치를
        # 가리키는 반쪽 상태를 남기지 않기 위해서다. rollback은 실패해도 무시(이미 예외 전파 중).
        try:
            conn.rollback()
        except Exception:
            pass
        shutil.move(str(dst), str(src))
        raise

    return {
        "project": name, "documents": moved_docs, "mappings": moved_maps,
        "hint": (
            f"config.yaml의 para.projects에서 '{name}'을 제거하세요 — 활성 목록에 남아 있으면 "
            f"새 문서가 다시 Projects/{name}로 분류될 수 있습니다"
        ),
    }
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_archive.py -v` → PASS, `./.venv/bin/pytest -q` 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/archive.py tests/test_archive.py
git commit -m "feat: archive_project — Projects→Archives 폴더 이동 + 인덱스/para_map 갱신, 실패 시 이동 롤백"
```

---

### Task 7: Archive API + UI + 문서

**Files:**
- Modify: `src/llmsearch/web/app.py`, `src/llmsearch/web/static/index.html`, `README.md`
- Test: `tests/test_web.py` (추가)

**Interfaces:**
- Consumes: Task 6의 `archive_project` (예외 계약: ValueError→400, KeyError→404)
- Produces: `GET /api/para/projects` → `[{"name": str, "doc_count": int}]` (summaries/Projects/ 하위 디렉터리 기준); `POST /api/archive` `{"project": str}` → archive_project 반환 dict. 둘 다 `sync_lock` 안에서 쓰기 conn 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_web.py`에 추가:

```python
def test_archive_api_moves_project(tmp_path):
    from fastapi.testclient import TestClient

    from llmsearch.config import Config
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app

    cfg = Config(data_dir=tmp_path / "data")
    proj = cfg.summaries_dir / "Projects" / "알파"
    proj.mkdir(parents=True)
    (proj / "요약.md").write_text("# x", encoding="utf-8")
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    # TrustedHostMiddleware가 기본 Host("testserver")를 거부하므로 base_url 필수
    client = TestClient(app, base_url="http://127.0.0.1")

    listed = client.get("/api/para/projects").json()
    assert listed == [{"name": "알파", "doc_count": 0}]

    r = client.post("/api/archive", json={"project": "알파"})
    assert r.status_code == 200
    assert "config.yaml" in r.json()["hint"]
    assert (cfg.summaries_dir / "Archives" / "알파" / "요약.md").exists()
    assert client.get("/api/para/projects").json() == []


def test_archive_api_unknown_project_404(tmp_path):
    from fastapi.testclient import TestClient

    from llmsearch.config import Config
    from llmsearch.embeddings import FakeEmbeddings
    from llmsearch.llm import FakeAnswerer
    from llmsearch.summarize import FakeSummarizer
    from llmsearch.web.app import create_app

    app = create_app(Config(data_dir=tmp_path / "data"), embedder=FakeEmbeddings(),
                     summarizer=FakeSummarizer(), answerer=FakeAnswerer(), enable_scheduler=False)
    client = TestClient(app, base_url="http://127.0.0.1")  # TrustedHost 통과
    assert client.post("/api/archive", json={"project": "없음"}).status_code == 404
    assert client.post("/api/archive", json={"project": ".."}).status_code == 400
```

(이 파일의 기존 create_app 헬퍼가 있으면 그것을 재사용하고 위 인라인 구성은 그 관례에 맞춰 조정한다.)

- [ ] **Step 2: 실패 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v -k archive`
Expected: FAIL — 404 (라우트 없음)

- [ ] **Step 3: 구현**

`src/llmsearch/web/app.py` — import에 `from ..archive import archive_project` 추가, `/api/atlassian/registrations` 라우트들 아래에 추가:

```python
    @app.get("/api/para/projects")
    def para_projects():
        """summaries/Projects/ 하위 폴더 목록 — GUI 아카이브 섹션용 (스펙 §7.1 P1)."""
        projects_dir = config.summaries_dir / "Projects"
        out = []
        if projects_dir.is_dir():
            for p in sorted(d for d in projects_dir.iterdir() if d.is_dir()):
                row = read_conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE para_path=?", (f"Projects/{p.name}",)
                ).fetchone()
                out.append({"name": p.name, "doc_count": row[0]})
        return out

    @app.post("/api/archive")
    def archive(payload: dict):
        name = str(payload.get("project", ""))
        with state["sync_lock"]:  # 동기화 중 폴더 이동 금지 — 쓰기 직렬화
            try:
                return archive_project(conn, config.summaries_dir, name)
            except KeyError as exc:
                raise HTTPException(404, exc.args[0])  # str(KeyError)는 따옴표가 붙어 UI에 그대로 노출됨
            except ValueError as exc:
                raise HTTPException(400, str(exc))
```

`src/llmsearch/web/static/index.html` — 소스 탭의 `<ul id="atlList"></ul>` 아래에 추가:

```html
  <h3>프로젝트 아카이브</h3>
  <ul id="projList"></ul>
```

스크립트의 `show(id)`에서 sources 분기를 다음으로 교체:

```javascript
  if (id === 'sources') { loadSources(); loadRegistrations(); loadProjects(); }
```

`loadRegistrations` 아래에 함수 추가:

```javascript
async function loadProjects() {
  const ps = await (await fetch('/api/para/projects')).json();
  document.getElementById('projList').innerHTML = ps.map(p =>
    `<li>${esc(p.name)} (${esc(p.doc_count)}건) ` +
    `<button onclick="archiveProject(this.dataset.n)" data-n="${esc(p.name)}">완료 처리</button></li>`).join('');
}
async function archiveProject(name) {
  if (!confirm(`'${name}' 프로젝트를 Archives로 이동할까요? 검색 순위가 하향됩니다.`)) return;
  const r = await fetch('/api/archive', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({project: name})});
  const data = await r.json();
  alert(r.ok ? (data.hint || '완료') : (data.detail || '실패'));
  loadProjects(); loadSources();
}
```

`README.md`에 아카이브 섹션 추가 (Atlassian 섹션 뒤):

```markdown
## 프로젝트 아카이브 (PARA)

소스 탭의 "프로젝트 아카이브"에서 완료된 프로젝트를 `완료 처리`하면
`summaries/Projects/<이름>/` 폴더가 `summaries/Archives/<이름>/`으로 이동하고
인덱스가 함께 갱신된다. Archives 문서는 검색에서 제외되지 않고 순위만 하향된다.
완료 처리 후 `config.yaml`의 `para.projects`에서 해당 프로젝트를 제거할 것 —
활성 목록에 남아 있으면 새 문서가 다시 그 프로젝트로 분류될 수 있다.
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/bin/pytest tests/test_web.py -v` → PASS, `./.venv/bin/pytest -q` 전체 green

- [ ] **Step 5: Commit**

```bash
git add src/llmsearch/web/app.py src/llmsearch/web/static/index.html README.md tests/test_web.py
git commit -m "feat: 프로젝트 아카이브 GUI — /api/para/projects, /api/archive, 완료 처리 버튼"
```

---

## M4 수동 체크리스트 (Windows / 실환경 — 머지 후 사용자 확인)

1. `python scripts/check_ppt_render.py <이미지 위주 pptx>` — PowerPoint COM 렌더링·PNG 시그니처 확인. 부분 손상 pptx로도 한 번 실행해 모달 없이(DisplayAlerts 억제) 실패·복구되는지 확인. POWERPNT.EXE가 실행 후 상주하는 것은 의도된 동작(사용자 세션 보호 — Quit 안 함)
2. 이미지 위주 pptx 1개를 watch 폴더에 넣고 local_docs 동기화 → 요약 md에 "슬라이드 비전 설명" 섹션 생성 확인 (Gemini 유료 티어 키 필요)
3. Confluence·Jira 자격증명이 다른 경우: `.env`에 `CONFLUENCE_PAT`/`JIRA_PAT` 분리 설정 후 각각 동기화 성공 확인
4. GUI에서 테스트 프로젝트 완료 처리 → `summaries/Archives/` 이동·검색 순위 하향·config.yaml 안내 확인
