# llmsearch M1 구현 계획 (코어 검증)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스펙 M1 — 하이브리드 인덱스 + 에이전틱 검색 + 채팅 GUI + 커넥터 2종(notes, local_docs) + 골든 평가. 완료 시점부터 로컬 Markdown/문서 검색 실사용 가능.

**Architecture:** 단일 Python 패키지 `llmsearch`. 커넥터 → 인덱서(SQLite FTS5 + 벡터) → 검색 툴 → Claude 에이전틱 답변 루프 → FastAPI 웹 GUI. 모든 LLM/임베딩 호출은 Protocol 인터페이스 뒤에 두고 테스트는 Fake 구현으로 수행 — 테스트에 API 키 불필요.

**Tech Stack:** Python 3.11+, FastAPI + uvicorn, SQLite(FTS5 + sqlite-vec, numpy 폴백), markitdown, anthropic SDK(답변: `claude-opus-5`), google-genai SDK(요약: Gemini Flash, 임베딩: 768차원 MRL), pytest.

**스펙:** `docs/superpowers/specs/2026-08-17-llmsearch-design.md` — 이 계획은 스펙 §4의 M1 범위만 다룬다. M2(Outlook), M3(Confluence/Jira)는 별도 계획. 스펙의 P1 중 **Archive 워크플로(GUI 완료 처리)**, **이미지 PPT 비전 보완**, **활성 목록 GUI 편집**은 M1에서 제외하고 M2 이후 계획에 편입한다 — M1에서 Projects/Areas 목록·Archive 이동은 config.yaml/파일 수동 관리로 충분하다.

## Global Constraints

- 실행 대상은 Windows Python이지만 **개발·테스트는 WSL에서 동작해야 한다**: Windows 전용 모듈 import 금지(M1은 해당 없음), 경로는 전부 `pathlib.Path`, 파일 IO는 `encoding="utf-8"` 명시
- LLM·임베딩 실호출 코드는 테스트에서 절대 실행되지 않는다 — 모든 테스트는 Fake 구현 사용
- SQLite는 WAL 모드, 쓰기는 인덱서 경로로만 (스펙 §5 P0)
- 임베딩 차원은 768 고정 (스펙 §8 P1)
- 웹서버는 `127.0.0.1` 바인딩 고정 (스펙 §9→10)
- API 키는 `.env`(`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`)에서만 읽는다 — config.yaml·코드에 평문 금지
- 커밋은 태스크마다 1회 이상, 메시지는 conventional commits(`feat:`, `test:`, `chore:`)

## 파일 구조 (M1 전체)

```
pyproject.toml                      # 패키지 정의 + 의존성
src/llmsearch/
├─ __init__.py
├─ models.py          # Document, SyncResult, Hit — 모든 계층의 공용 데이터형
├─ config.py          # config.yaml 로딩 (Config dataclass)
├─ rules.py           # 결정적 규칙 매칭(para_overrides/exclude) + rules.md 섹션 파서
├─ db.py              # 스키마 생성·마이그레이션 없음(재구축)·벡터 저장/검색(sqlite-vec 또는 numpy)
├─ chunking.py        # 문단 경계 우선 청킹
├─ embeddings.py      # EmbeddingProvider 프로토콜, FakeEmbeddings, GeminiEmbeddings
├─ indexer.py         # Document → 청크+FTS+벡터 upsert, 삭제 전파, sync_state
├─ search.py          # 하이브리드 RRF + 구조화 필터 + 랭킹 조정 + 문서 승격 → Hit
├─ summarize.py       # Summarizer 프로토콜, FakeSummarizer, GeminiSummarizer, PARA 분류
├─ connectors/
│  ├─ __init__.py
│  ├─ notes.py        # md 폴더 인덱싱 (변환 없음)
│  └─ local_docs.py   # markitdown 추출, DRM 폴백, 요약·분류·복사 파이프라인
├─ llm.py             # Answerer 프로토콜, FakeAnswerer, ClaudeAnswerer(에이전틱 루프)
├─ web/
│  ├─ __init__.py
│  ├─ app.py          # FastAPI 라우트 + 스케줄러 + 앱 조립(create_app)
│  └─ static/index.html  # 채팅/소스/로그 탭 단일 페이지
└─ eval/
   ├─ __init__.py
   └─ golden.py       # 골든 질문 세트 상위3 적중률 측정
scripts/spike_sqlite_vec.py         # Windows에서 sqlite-vec 동작 확인용 (30분 스파이크)
tests/                              # 태스크별 test_*.py
```

각 파일은 위 한 줄 책임만 갖는다. 커넥터는 `indexer`·`summarize`만 사용하고 서로를 모른다. `web/app.py`만 전 계층을 조립한다.

---

### Task 1: 프로젝트 스캐폴드 + sqlite-vec 스파이크

**Files:**
- Create: `pyproject.toml`, `src/llmsearch/__init__.py`, `tests/__init__.py`, `tests/test_scaffold.py`, `scripts/spike_sqlite_vec.py`, `.gitignore`

**Interfaces:**
- Produces: 설치 가능한 `llmsearch` 패키지, `pytest` 실행 환경, `HAS_SQLITE_VEC` 판별 로직(후속 Task 4가 동일 패턴 사용)

- [ ] **Step 1: pyproject.toml 작성**

```toml
[project]
name = "llmsearch"
version = "0.1.0"
description = "개인용 통합 문서 검색 툴"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "pyyaml>=6.0",
    "numpy>=1.26",
    "markitdown[docx,pptx,xlsx,pdf]>=0.1",
    "anthropic>=0.60",
    "google-genai>=1.0",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
vec = ["sqlite-vec>=0.1"]
dev = ["pytest>=8.0", "httpx>=0.27"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
llmsearch = ["web/static/*.html"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: 패키지 스켈레톤과 .gitignore 작성**

`src/llmsearch/__init__.py`:
```python
__version__ = "0.1.0"
```

`.gitignore`:
```
__pycache__/
*.egg-info/
.env
.venv/
*.db
*.db-wal
*.db-shm
```

- [ ] **Step 3: 실패하는 테스트 작성** — `tests/test_scaffold.py`

```python
def test_package_importable():
    import llmsearch
    assert llmsearch.__version__ == "0.1.0"
```

- [ ] **Step 4: 설치 후 테스트 통과 확인**

Run: `pip install -e ".[dev,vec]" && pytest tests/test_scaffold.py -v`
Expected: PASS (sqlite-vec 설치 실패 시 `pip install -e ".[dev]"`로 재시도 — numpy 폴백 경로가 Task 4에 있으므로 진행 가능)

- [ ] **Step 5: 스파이크 스크립트 작성** — `scripts/spike_sqlite_vec.py` (Windows에서 수동 실행용, 자동 테스트 아님)

```python
"""sqlite-vec가 이 플랫폼에서 동작하는지 확인. 실패해도 numpy 폴백으로 앱은 동작한다."""
import sqlite3, struct

try:
    import sqlite_vec
except ImportError:
    print("sqlite-vec 미설치 -> numpy 폴백 사용")
    raise SystemExit(0)

db = sqlite3.connect(":memory:")
db.enable_load_extension(True)
sqlite_vec.load(db)
db.enable_load_extension(False)
db.execute("CREATE VIRTUAL TABLE v USING vec0(id INTEGER PRIMARY KEY, embedding float[4])")
db.execute("INSERT INTO v(id, embedding) VALUES (1, ?)", (struct.pack("4f", 1, 0, 0, 0),))
row = db.execute(
    "SELECT id, distance FROM v WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
    (struct.pack("4f", 1, 0, 0, 0),),
).fetchone()
assert row[0] == 1, row
print(f"sqlite-vec OK (version {db.execute('SELECT vec_version()').fetchone()[0]})")
```

Run: `python scripts/spike_sqlite_vec.py`
Expected: `sqlite-vec OK ...` 또는 폴백 안내 출력

- [ ] **Step 6: 커밋**

```bash
git add pyproject.toml src tests scripts .gitignore
git commit -m "chore: 프로젝트 스캐폴드 + sqlite-vec 스파이크"
```

---

### Task 2: 공용 데이터형과 설정 로딩

**Files:**
- Create: `src/llmsearch/models.py`, `src/llmsearch/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Document(source_type: str, source_id: str, title: str, text: str, url_or_path: str, updated_at: datetime, content_indexed: bool = True, extra: dict = {})`
  - `SyncResult(documents: list[Document], deleted_ids: list[str], state: dict)`
  - `Hit(source_type: str, source_id: str, title: str, url_or_path: str, updated_at: str, content_indexed: bool, score: float, excerpt: str)`
  - `Config` dataclass + `load_config(path: Path) -> Config`
- Consumes: 없음 (최하위 계층)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_config.py`

```python
from pathlib import Path
from llmsearch.config import load_config


def test_load_config(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
data_dir: /tmp/llmsearch-data
watch_folders: ["/docs/work"]
notes_folders: ["/notes"]
para:
  projects: ["프로젝트A"]
  areas: ["팀운영"]
rules:
  para_overrides:
    - match: "path:**/경영회의/**"
      target: "Areas/경영지원"
  exclude: ["folder:인사평가"]
sync_interval_minutes: 15
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.data_dir == Path("/tmp/llmsearch-data")
    assert cfg.watch_folders == [Path("/docs/work")]
    assert cfg.projects == ["프로젝트A"]
    assert cfg.areas == ["팀운영"]
    assert cfg.para_overrides[0]["target"] == "Areas/경영지원"
    assert cfg.exclude == ["folder:인사평가"]
    assert cfg.sync_interval_minutes == 15
    assert cfg.answer_model == "claude-opus-5"  # 기본값


def test_load_config_defaults(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("data_dir: /d\n", encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.sync_interval_minutes == 30
    assert cfg.watch_folders == []
    assert cfg.para_overrides == []
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: llmsearch.config`

- [ ] **Step 3: 구현** — `src/llmsearch/models.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Document:
    """모든 커넥터가 인덱서에 넘기는 공용 문서 표현 (스펙 §6)."""

    source_type: str        # "notes" | "local_docs" | (M2+) "outlook_mail" ...
    source_id: str          # 소스 내 고유 ID (파일 절대경로 등)
    title: str
    text: str               # 인덱싱 대상 본문 (DRM 문서는 메타데이터 설명문)
    url_or_path: str        # 원본 열기용
    updated_at: datetime
    content_indexed: bool = True  # False = DRM 등으로 메타데이터만 인덱싱됨
    extra: dict = field(default_factory=dict)


@dataclass
class SyncResult:
    documents: list[Document]
    deleted_ids: list[str]  # 소스에서 사라진 source_id 목록
    state: dict             # 다음 동기화에 넘길 커서 (커넥터 자유 형식)


@dataclass
class Hit:
    """검색 결과 1건 — 문서 단위로 승격된 상태 (스펙 §8)."""

    source_type: str
    source_id: str
    title: str
    url_or_path: str
    updated_at: str         # ISO 문자열
    content_indexed: bool
    score: float
    excerpt: str            # 승격된 문서 본문 (상한 6000자)
```

`src/llmsearch/config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    data_dir: Path
    watch_folders: list[Path] = field(default_factory=list)
    notes_folders: list[Path] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    areas: list[str] = field(default_factory=list)
    para_overrides: list[dict] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    sync_interval_minutes: int = 30
    answer_model: str = "claude-opus-5"
    summary_model: str = "gemini-flash-latest"
    embed_model: str = "gemini-embedding-001"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def summaries_dir(self) -> Path:
        return self.data_dir / "summaries"

    @property
    def rules_md_path(self) -> Path:
        return self.data_dir / "rules.md"


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    para = raw.get("para", {})
    rules = raw.get("rules", {})
    return Config(
        data_dir=Path(raw["data_dir"]),
        watch_folders=[Path(p) for p in raw.get("watch_folders", [])],
        notes_folders=[Path(p) for p in raw.get("notes_folders", [])],
        projects=list(para.get("projects", [])),
        areas=list(para.get("areas", [])),
        para_overrides=list(rules.get("para_overrides", [])),
        exclude=list(rules.get("exclude", [])),
        sync_interval_minutes=int(raw.get("sync_interval_minutes", 30)),
        answer_model=raw.get("answer_model", "claude-opus-5"),
        summary_model=raw.get("summary_model", "gemini-flash-latest"),
        embed_model=raw.get("embed_model", "gemini-embedding-001"),
    )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_config.py -v`
Expected: PASS (2건)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/models.py src/llmsearch/config.py tests/test_config.py
git commit -m "feat: 공용 데이터형(Document/SyncResult/Hit)과 config.yaml 로딩"
```

---

### Task 3: 사용자 규칙 — 결정적 규칙 매칭 + rules.md 파서

**Files:**
- Create: `src/llmsearch/rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Produces:
  - `match_override(path: str | None, sender: str | None, overrides: list[dict]) -> str | None` — 첫 매칭 `target` 반환
  - `is_excluded(path: str | None, sender: str | None, folder: str | None, excludes: list[str]) -> bool`
  - `load_rules_md(path: Path) -> dict[str, str]` — `## 섹션명` → 본문. 파일 없으면 `{}`
- Consumes: Task 2의 `Config.para_overrides`, `Config.exclude`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_rules.py`

```python
from pathlib import Path
from llmsearch.rules import is_excluded, load_rules_md, match_override

OVERRIDES = [
    {"match": "path:**/경영회의/**", "target": "Areas/경영지원"},
    {"match": "sender:*@partner-x.com", "target": "Projects/파트너X협업"},
]


def test_match_override_path():
    assert match_override("/docs/2026/경영회의/1월.pptx", None, OVERRIDES) == "Areas/경영지원"


def test_match_override_sender():
    assert match_override(None, "kim@partner-x.com", OVERRIDES) == "Projects/파트너X협업"


def test_match_override_none():
    assert match_override("/docs/기타.pptx", "a@b.com", OVERRIDES) is None


def test_is_excluded_folder():
    assert is_excluded("/mail/인사평가/x.msg", None, "인사평가", ["folder:인사평가"])
    assert not is_excluded("/mail/일반/x.msg", None, "일반", ["folder:인사평가"])


def test_load_rules_md(tmp_path: Path):
    f = tmp_path / "rules.md"
    f.write_text("## 용어집\nTF-N은 차세대 TF다.\n\n## 답변 규칙\n두괄식으로.\n", encoding="utf-8")
    sections = load_rules_md(f)
    assert "TF-N" in sections["용어집"]
    assert sections["답변 규칙"] == "두괄식으로."


def test_load_rules_md_missing(tmp_path: Path):
    assert load_rules_md(tmp_path / "none.md") == {}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_rules.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/rules.py`

```python
from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath


def _match_one(rule: str, path: str | None, sender: str | None, folder: str | None) -> bool:
    kind, _, pattern = rule.partition(":")
    if kind == "path" and path is not None:
        # 경로 구분자를 통일해 Windows/POSIX 양쪽에서 동일하게 매칭
        norm = str(PurePosixPath(Path(path).as_posix()))
        return fnmatch.fnmatch(norm, pattern)
    if kind == "sender" and sender is not None:
        return fnmatch.fnmatch(sender.lower(), pattern.lower())
    if kind == "folder" and folder is not None:
        return fnmatch.fnmatch(folder, pattern)
    return False


def match_override(path: str | None, sender: str | None, overrides: list[dict]) -> str | None:
    for rule in overrides:
        if _match_one(rule["match"], path, sender, None):
            return rule["target"]
    return None


def is_excluded(path: str | None, sender: str | None, folder: str | None, excludes: list[str]) -> bool:
    return any(_match_one(rule, path, sender, folder) for rule in excludes)


def load_rules_md(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_rules.py -v`
Expected: PASS (6건)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/rules.py tests/test_rules.py
git commit -m "feat: 결정적 규칙 매칭(para_overrides/exclude)과 rules.md 섹션 파서"
```

---

### Task 4: 인덱스 DB — 스키마, WAL, 벡터 저장/검색(폴백 포함)

**Files:**
- Create: `src/llmsearch/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `open_db(path: Path) -> sqlite3.Connection` — WAL 활성, 스키마 생성, `schema_version` 기록. 버전 불일치 시 `SchemaMismatchError` (재인덱싱 안내용)
  - `insert_embedding(conn, chunk_id: int, vector: list[float]) -> None`
  - `search_embeddings(conn, query: list[float], k: int) -> list[tuple[int, float]]` — `(chunk_id, distance)` 오름차순
  - `HAS_SQLITE_VEC: bool`, `SCHEMA_VERSION: int`, `class SchemaMismatchError(Exception)`
- 테이블: `documents(id, source_type, source_id, title, url_or_path, updated_at, content_indexed, para_path, extra_json, UNIQUE(source_type, source_id))`, `chunks(id, doc_id, seq, text)`, `chunks_fts`(FTS5, external content), `sync_state(source_type PK, state_json)`, `para_map(source_id PK, para_path, summary_path)`, `meta(key PK, value)`, 벡터: `chunk_vecs`(vec0) 또는 `chunk_vecs_np(chunk_id PK, embedding BLOB)`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_db.py`

```python
import struct
from pathlib import Path

import pytest
from llmsearch import db


def test_open_db_creates_schema(tmp_path: Path):
    conn = db.open_db(tmp_path / "index.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','virtual table') OR type='table'")}
    for t in ("documents", "chunks", "sync_state", "para_map", "meta"):
        assert t in tables, t
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # FTS5 가상 테이블 동작 확인
    conn.execute("INSERT INTO chunks(doc_id, seq, text) VALUES (1, 0, '프로젝트A 회의록')")
    conn.execute("INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks")
    rows = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '프로젝트A'").fetchall()
    assert len(rows) == 1


def test_schema_version_mismatch(tmp_path: Path):
    p = tmp_path / "index.db"
    conn = db.open_db(p)
    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(db.SchemaMismatchError):
        db.open_db(p)


def _vec(*head: float) -> list[float]:
    """앞자리만 지정한 768차원 벡터 — vec0 float[768] 컬럼과 차원 일치 필수."""
    v = [0.0] * 768
    for i, x in enumerate(head):
        v[i] = x
    return v


def test_embedding_roundtrip(tmp_path: Path):
    conn = db.open_db(tmp_path / "index.db")
    db.insert_embedding(conn, 1, _vec(1.0))
    db.insert_embedding(conn, 2, _vec(0.0, 1.0))
    conn.commit()
    results = db.search_embeddings(conn, _vec(0.9, 0.1), k=2)
    assert results[0][0] == 1  # 가장 가까운 청크가 먼저
    assert len(results) == 2
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/db.py`

```python
from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import numpy as np

try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

SCHEMA_VERSION = 1


class SchemaMismatchError(Exception):
    """index.db 스키마 버전 불일치 — 인덱스는 소모품이므로 rebuild로 해결한다 (스펙 §8)."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url_or_path TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content_indexed INTEGER NOT NULL DEFAULT 1,
    para_path TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_type, source_id)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id');
CREATE TABLE IF NOT EXISTS sync_state (source_type TEXT PRIMARY KEY, state_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS para_map (source_id TEXT PRIMARY KEY, para_path TEXT NOT NULL, summary_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if HAS_SQLITE_VEC:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.executescript(_SCHEMA)
    if HAS_SQLITE_VEC:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vecs USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[768])"
        )
    else:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunk_vecs_np (chunk_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)"
        )
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    elif int(row[0]) != SCHEMA_VERSION:
        conn.close()
        raise SchemaMismatchError(
            f"index.db schema v{row[0]} != v{SCHEMA_VERSION}. index.db를 삭제하고 재인덱싱하세요."
        )
    return conn


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def insert_embedding(conn: sqlite3.Connection, chunk_id: int, vector: list[float]) -> None:
    if HAS_SQLITE_VEC:
        conn.execute(
            "INSERT OR REPLACE INTO chunk_vecs(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, _pack(vector)),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO chunk_vecs_np(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, _pack(vector)),
        )


def delete_embeddings(conn: sqlite3.Connection, chunk_ids: list[int]) -> None:
    table = "chunk_vecs" if HAS_SQLITE_VEC else "chunk_vecs_np"
    conn.executemany(f"DELETE FROM {table} WHERE chunk_id=?", [(c,) for c in chunk_ids])


def search_embeddings(conn: sqlite3.Connection, query: list[float], k: int) -> list[tuple[int, float]]:
    if HAS_SQLITE_VEC:
        rows = conn.execute(
            "SELECT chunk_id, distance FROM chunk_vecs WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (_pack(query), k),
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]
    # numpy 브루트포스 폴백 — 중규모(수십만 청크 미만)에서 충분히 빠름
    rows = conn.execute("SELECT chunk_id, embedding FROM chunk_vecs_np").fetchall()
    if not rows:
        return []
    ids = np.array([r[0] for r in rows])
    mat = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    q = np.asarray(query, dtype=np.float32)
    dists = np.linalg.norm(mat - q, axis=1)
    order = np.argsort(dists)[:k]
    return [(int(ids[i]), float(dists[i])) for i in order]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_db.py -v`
Expected: PASS (3건) — sqlite-vec 유무와 무관하게 통과해야 함

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/db.py tests/test_db.py
git commit -m "feat: 인덱스 DB 스키마(WAL/FTS5/벡터, numpy 폴백, schema_version)"
```

---

### Task 5: 청킹과 임베딩 프로바이더

**Files:**
- Create: `src/llmsearch/chunking.py`, `src/llmsearch/embeddings.py`
- Test: `tests/test_chunking.py`, `tests/test_embeddings.py`

**Interfaces:**
- Produces:
  - `chunk_text(text: str, max_chars: int = 800) -> list[str]` — 문단 경계 우선 분할
  - `class EmbeddingProvider(Protocol): def embed(self, texts: list[str]) -> list[list[float]]`
  - `FakeEmbeddings(dim: int = 768)` — 결정적 해시 기반 (테스트용)
  - `GeminiEmbeddings(model: str, dim: int = 768)` — google-genai, 배치 호출, MRL 절단
- Consumes: 없음

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_chunking.py`

```python
from llmsearch.chunking import chunk_text


def test_short_text_single_chunk():
    assert chunk_text("짧은 글") == ["짧은 글"]


def test_splits_on_paragraphs():
    text = "\n\n".join(f"문단{i} " + "가" * 300 for i in range(5))
    chunks = chunk_text(text, max_chars=800)
    assert all(len(c) <= 800 for c in chunks)
    assert len(chunks) >= 2
    assert chunks[0].startswith("문단0")


def test_long_paragraph_hard_split():
    chunks = chunk_text("가" * 2000, max_chars=800)
    assert all(len(c) <= 800 for c in chunks)
    assert "".join(chunks) == "가" * 2000


def test_empty():
    assert chunk_text("") == []
```

`tests/test_embeddings.py`:

```python
from llmsearch.embeddings import FakeEmbeddings


def test_fake_embeddings_deterministic():
    e = FakeEmbeddings(dim=8)
    v1 = e.embed(["안녕", "하이"])
    v2 = e.embed(["안녕", "하이"])
    assert v1 == v2
    assert len(v1) == 2 and len(v1[0]) == 8


def test_fake_embeddings_similar_text_closer():
    e = FakeEmbeddings(dim=64)
    a, b, c = e.embed(["프로젝트A 회의록", "프로젝트A 회의 기록", "점심 메뉴"])
    def dist(x, y):
        return sum((i - j) ** 2 for i, j in zip(x, y))
    assert dist(a, b) < dist(a, c)  # 토큰 겹침이 많을수록 가깝다
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_chunking.py tests/test_embeddings.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/chunking.py`

```python
from __future__ import annotations


def chunk_text(text: str, max_chars: int = 800) -> list[str]:
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        while len(p) > max_chars:  # 문단 자체가 상한 초과 → 강제 분할
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(p[:max_chars])
            p = p[max_chars:]
        if buf and len(buf) + 2 + len(p) > max_chars:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks
```

`src/llmsearch/embeddings.py`:

```python
from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddings:
    """결정적 토큰 해시 임베딩 — 테스트·오프라인 개발용. 토큰 겹침 = 유사도."""

    def __init__(self, dim: int = 768):
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = int.from_bytes(hashlib.md5(token.encode()).digest()[:4], "big")
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class GeminiEmbeddings:
    """Gemini 임베딩 API — 768차원 MRL 절단, 100건 배치 (스펙 §8)."""

    def __init__(self, model: str = "gemini-embedding-001", dim: int = 768):
        from google import genai  # 지연 import — 테스트 환경에 키 불필요

        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        from google.genai import types

        out: list[list[float]] = []
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            resp = self.client.models.embed_content(
                model=self.model,
                contents=batch,
                config=types.EmbedContentConfig(output_dimensionality=self.dim),
            )
            out.extend([list(e.values) for e in resp.embeddings])
        return out
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_chunking.py tests/test_embeddings.py -v`
Expected: PASS (6건). `GeminiEmbeddings`는 테스트하지 않음(실 API) — 생성자에서 지연 import이므로 키 없이도 모듈 import는 성공해야 함

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/chunking.py src/llmsearch/embeddings.py tests/test_chunking.py tests/test_embeddings.py
git commit -m "feat: 문단 경계 청킹 + 임베딩 프로바이더(Fake/Gemini 768차원)"
```

---

### Task 6: 인덱서 — upsert, 삭제 전파, sync_state

**Files:**
- Create: `src/llmsearch/indexer.py`
- Test: `tests/test_indexer.py`

**Interfaces:**
- Consumes: `db.open_db/insert_embedding/delete_embeddings`, `chunk_text`, `EmbeddingProvider`, `Document`
- Produces:
  - `index_documents(conn, docs: list[Document], embedder: EmbeddingProvider) -> int` — upsert(기존 동일 source 문서는 청크·벡터 삭제 후 재삽입), 반환값은 처리 문서 수
  - `delete_documents(conn, source_type: str, source_ids: list[str]) -> int`
  - `get_sync_state(conn, source_type: str) -> dict` / `set_sync_state(conn, source_type: str, state: dict) -> None`
  - `set_para_map(conn, source_id, para_path, summary_path)` / `get_para_map(conn, source_id) -> tuple[str, str] | None`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_indexer.py`

```python
from datetime import datetime
from pathlib import Path

from llmsearch import db, indexer
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.models import Document


def make_doc(sid: str, text: str) -> Document:
    return Document(
        source_type="notes", source_id=sid, title=sid, text=text,
        url_or_path=f"/n/{sid}", updated_at=datetime(2026, 8, 1),
    )


def test_index_and_reindex(tmp_path: Path):
    conn = db.open_db(tmp_path / "i.db")
    emb = FakeEmbeddings(dim=768)
    n = indexer.index_documents(conn, [make_doc("a.md", "프로젝트A 킥오프 회의록")], emb)
    assert n == 1
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert chunk_count >= 1
    # 같은 문서 재인덱싱 → 중복 없이 교체
    indexer.index_documents(conn, [make_doc("a.md", "수정된 회의록 본문")], emb)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    row = conn.execute("SELECT text FROM chunks LIMIT 1").fetchone()
    assert "수정된" in row[0]
    # FTS 동기화 확인
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH '수정된'").fetchone()[0] == 1


def test_delete_documents(tmp_path: Path):
    conn = db.open_db(tmp_path / "i.db")
    emb = FakeEmbeddings(dim=768)
    indexer.index_documents(conn, [make_doc("a.md", "본문"), make_doc("b.md", "본문2")], emb)
    deleted = indexer.delete_documents(conn, "notes", ["a.md"])
    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH '본문'").fetchone()[0] == 1


def test_sync_state_roundtrip(tmp_path: Path):
    conn = db.open_db(tmp_path / "i.db")
    assert indexer.get_sync_state(conn, "notes") == {}
    indexer.set_sync_state(conn, "notes", {"files": {"a.md": 123.0}})
    assert indexer.get_sync_state(conn, "notes")["files"]["a.md"] == 123.0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_indexer.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/indexer.py`

```python
from __future__ import annotations

import json
import sqlite3

from .chunking import chunk_text
from .db import delete_embeddings, insert_embedding
from .embeddings import EmbeddingProvider
from .models import Document


def _delete_doc_rows(conn: sqlite3.Connection, doc_id: int) -> None:
    chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
    for cid in chunk_ids:
        conn.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', ?, (SELECT text FROM chunks WHERE id=?))", (cid, cid))
    delete_embeddings(conn, chunk_ids)
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))


def index_documents(conn: sqlite3.Connection, docs: list[Document], embedder: EmbeddingProvider) -> int:
    for doc in docs:
        row = conn.execute(
            "SELECT id FROM documents WHERE source_type=? AND source_id=?",
            (doc.source_type, doc.source_id),
        ).fetchone()
        if row:
            _delete_doc_rows(conn, row[0])
        cur = conn.execute(
            "INSERT INTO documents(source_type, source_id, title, url_or_path, updated_at, content_indexed, extra_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (doc.source_type, doc.source_id, doc.title, doc.url_or_path,
             doc.updated_at.isoformat(), int(doc.content_indexed), json.dumps(doc.extra, ensure_ascii=False)),
        )
        doc_id = cur.lastrowid
        # 청크 헤더에 제목·날짜 포함 (스펙 §8 청킹)
        header = f"[{doc.title} | {doc.updated_at.date().isoformat()}] "
        chunks = chunk_text(doc.text) or [doc.title]
        texts = [header + c for c in chunks]
        vectors = embedder.embed(texts)
        for seq, (text, vec) in enumerate(zip(texts, vectors)):
            cur = conn.execute("INSERT INTO chunks(doc_id, seq, text) VALUES (?,?,?)", (doc_id, seq, text))
            cid = cur.lastrowid
            conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)", (cid, text))
            insert_embedding(conn, cid, vec)
    conn.commit()
    return len(docs)


def delete_documents(conn: sqlite3.Connection, source_type: str, source_ids: list[str]) -> int:
    count = 0
    for sid in source_ids:
        row = conn.execute(
            "SELECT id FROM documents WHERE source_type=? AND source_id=?", (source_type, sid)
        ).fetchone()
        if row:
            _delete_doc_rows(conn, row[0])
            count += 1
    conn.commit()
    return count


def get_sync_state(conn: sqlite3.Connection, source_type: str) -> dict:
    row = conn.execute("SELECT state_json FROM sync_state WHERE source_type=?", (source_type,)).fetchone()
    return json.loads(row[0]) if row else {}


def set_sync_state(conn: sqlite3.Connection, source_type: str, state: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sync_state(source_type, state_json) VALUES (?,?)",
        (source_type, json.dumps(state, ensure_ascii=False)),
    )
    conn.commit()


def set_para_map(conn: sqlite3.Connection, source_id: str, para_path: str, summary_path: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO para_map(source_id, para_path, summary_path) VALUES (?,?,?)",
        (source_id, para_path, summary_path),
    )
    conn.commit()


def get_para_map(conn: sqlite3.Connection, source_id: str) -> tuple[str, str] | None:
    row = conn.execute("SELECT para_path, summary_path FROM para_map WHERE source_id=?", (source_id,)).fetchone()
    return (row[0], row[1]) if row else None
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_indexer.py -v`
Expected: PASS (3건)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/indexer.py tests/test_indexer.py
git commit -m "feat: 인덱서 — upsert/삭제 전파/sync_state/para_map"
```

---

### Task 7: 하이브리드 검색 — RRF, 필터, 랭킹 조정, 문서 승격

**Files:**
- Create: `src/llmsearch/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: `db.search_embeddings`, `EmbeddingProvider`, `Hit`
- Produces:
  - `search(conn, embedder, query: str, source_filter: list[str] | None = None, date_from: str | None = None, date_to: str | None = None, sender: str | None = None, k: int = 12) -> list[Hit]`
  - 내부 규칙: 벡터 상위 30 + FTS 상위 30 → RRF(공식 `1/(60+rank)`) → 문서당 청크 상한 3 → 문서 점수 = 소속 청크 RRF 합 × 최신성 부스트 × Archive 감쇠(0.5) → 상위 k 문서 → `excerpt`는 문서 청크 전체 연결, 6000자 초과 시 최고 청크 주변 발췌
  - 질의 임베딩 캐시: 모듈 수준 `functools.lru_cache` 불가(list 반환) → `search` 내부 dict 캐시 `_QUERY_CACHE`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_search.py`

```python
from datetime import datetime, timedelta
from pathlib import Path

from llmsearch import db, indexer, search
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.models import Document

EMB = FakeEmbeddings(dim=768)


def setup_index(tmp_path: Path):
    conn = db.open_db(tmp_path / "s.db")
    now = datetime(2026, 8, 15)
    docs = [
        Document("notes", "kickoff.md", "프로젝트A 킥오프", "프로젝트A 킥오프 회의록. 일정과 담당자 결정.",
                 "/n/kickoff.md", now, extra={"para_path": "Projects/프로젝트A"}),
        Document("notes", "lunch.md", "점심 기록", "오늘 점심은 김치찌개.", "/n/lunch.md", now),
        Document("notes", "old.md", "프로젝트A 과거 자료", "프로젝트A 초기 기획 메모.",
                 "/n/old.md", now - timedelta(days=700), extra={"para_path": "Archives/프로젝트A"}),
        Document("local_docs", "spec.pptx", "프로젝트A 발표자료", "프로젝트A 발표자료 요약. 로드맵 포함.",
                 "/d/spec.pptx", now),
    ]
    indexer.index_documents(conn, docs, EMB)
    # documents.para_path 반영 (인덱서는 extra로 받아 컬럼에 기록)
    return conn


def test_hybrid_search_finds_relevant(tmp_path: Path):
    conn = setup_index(tmp_path)
    hits = search.search(conn, EMB, "프로젝트A 킥오프 회의록")
    assert hits, "결과 없음"
    assert hits[0].source_id == "kickoff.md"
    ids = [h.source_id for h in hits]
    assert "lunch.md" not in ids[:2]


def test_source_filter(tmp_path: Path):
    conn = setup_index(tmp_path)
    hits = search.search(conn, EMB, "프로젝트A", source_filter=["local_docs"])
    assert hits and all(h.source_type == "local_docs" for h in hits)


def test_archive_decay(tmp_path: Path):
    conn = setup_index(tmp_path)
    hits = search.search(conn, EMB, "프로젝트A 기획")
    ids = [h.source_id for h in hits]
    # Archive 문서는 감쇠되어 활성 문서보다 아래 (제외는 아님)
    assert "old.md" in ids
    assert ids.index("old.md") > ids.index("kickoff.md")


def test_excerpt_capped(tmp_path: Path):
    conn = db.open_db(tmp_path / "s2.db")
    long_text = "\n\n".join(f"섹션{i} 프로젝트B 내용 " + "가" * 500 for i in range(30))
    indexer.index_documents(
        conn, [Document("notes", "big.md", "긴 문서", long_text, "/n/big.md", datetime(2026, 8, 1))], EMB
    )
    hits = search.search(conn, EMB, "프로젝트B 섹션5")
    assert hits and len(hits[0].excerpt) <= 6000
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_search.py -v`
Expected: FAIL — `ModuleNotFoundError` (또는 첫 테스트에서 `AttributeError`)

- [ ] **Step 3: 구현** — `src/llmsearch/search.py`
  (주의: 인덱서가 `extra["para_path"]`를 `documents.para_path` 컬럼에 기록하도록 이 태스크에서 `indexer.py`의 INSERT를 수정한다 — `doc.extra.get("para_path")`를 컬럼에 추가)

`indexer.py` INSERT 수정 (documents INSERT에 `para_path` 추가):

```python
        cur = conn.execute(
            "INSERT INTO documents(source_type, source_id, title, url_or_path, updated_at, content_indexed, para_path, extra_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (doc.source_type, doc.source_id, doc.title, doc.url_or_path,
             doc.updated_at.isoformat(), int(doc.content_indexed),
             doc.extra.get("para_path"), json.dumps(doc.extra, ensure_ascii=False)),
        )
```

`src/llmsearch/search.py`:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime

from .db import search_embeddings
from .embeddings import EmbeddingProvider
from .models import Hit

RRF_K = 60
CANDIDATES = 30
PER_DOC_CAP = 3
EXCERPT_CAP = 6000
_QUERY_CACHE: dict[str, list[float]] = {}


def _fts_query(query: str) -> str:
    # FTS5 특수문자 제거 후 OR 매칭 — 정확 구문보다 재현율 우선
    tokens = ["".join(ch for ch in t if ch.isalnum()) for t in query.split()]
    tokens = [t for t in tokens if t]
    return " OR ".join(f'"{t}"' for t in tokens) if tokens else '""'


def _recency_boost(updated_at: str, now: datetime) -> float:
    try:
        dt = datetime.fromisoformat(updated_at)
    except ValueError:
        return 1.0
    days = max((now - dt).days, 0)
    return 1.0 + 0.3 * max(0.0, 1.0 - days / 365)  # 1년 내 문서에 최대 +30%


def search(
    conn: sqlite3.Connection,
    embedder: EmbeddingProvider,
    query: str,
    source_filter: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sender: str | None = None,
    k: int = 12,
) -> list[Hit]:
    if query not in _QUERY_CACHE:
        if len(_QUERY_CACHE) > 512:
            _QUERY_CACHE.clear()
        _QUERY_CACHE[query] = embedder.embed([query])[0]
    qvec = _QUERY_CACHE[query]

    vec_hits = search_embeddings(conn, qvec, CANDIDATES)          # [(chunk_id, dist)]
    fts_rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
        (_fts_query(query), CANDIDATES),
    ).fetchall()

    rrf: dict[int, float] = {}
    for rank, (cid, _) in enumerate(vec_hits):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (cid,) in enumerate(fts_rows):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    if not rrf:
        return []

    placeholders = ",".join("?" * len(rrf))
    rows = conn.execute(
        f"""SELECT c.id, c.doc_id, d.source_type, d.source_id, d.title, d.url_or_path,
                   d.updated_at, d.content_indexed, d.para_path, d.extra_json
            FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({placeholders})""",
        list(rrf),
    ).fetchall()

    now = datetime.now()
    doc_scores: dict[int, float] = {}
    doc_meta: dict[int, tuple] = {}
    doc_best_chunk: dict[int, int] = {}
    doc_chunk_count: dict[int, int] = {}
    for cid, doc_id, stype, sid, title, url, updated, cidx, para, extra in rows:
        import json as _json
        ex = _json.loads(extra)
        if source_filter and stype not in source_filter:
            continue
        if date_from and updated < date_from:
            continue
        if date_to and updated > date_to:
            continue
        if sender and ex.get("sender", "").lower() != sender.lower():
            continue
        if doc_chunk_count.get(doc_id, 0) >= PER_DOC_CAP:
            continue
        doc_chunk_count[doc_id] = doc_chunk_count.get(doc_id, 0) + 1
        score = rrf[cid]
        if doc_id not in doc_best_chunk or score > rrf.get(doc_best_chunk[doc_id], 0):
            doc_best_chunk[doc_id] = cid
        doc_scores[doc_id] = doc_scores.get(doc_id, 0.0) + score
        doc_meta[doc_id] = (stype, sid, title, url, updated, cidx, para)

    for doc_id, (stype, sid, title, url, updated, cidx, para) in doc_meta.items():
        boost = _recency_boost(updated, now)
        if para and para.startswith("Archives/"):
            boost *= 0.5  # Archive 감쇠 — 제외가 아닌 하향 (스펙 §8 P1)
        doc_scores[doc_id] *= boost

    top = sorted(doc_scores, key=doc_scores.get, reverse=True)[:k]
    hits: list[Hit] = []
    for doc_id in top:
        stype, sid, title, url, updated, cidx, para = doc_meta[doc_id]
        chunk_rows = conn.execute(
            "SELECT id, text FROM chunks WHERE doc_id=? ORDER BY seq", (doc_id,)
        ).fetchall()
        full = "\n".join(t for _, t in chunk_rows)
        if len(full) > EXCERPT_CAP:  # 최고 청크 주변 발췌 (스펙 §8)
            best = doc_best_chunk[doc_id]
            idx = next((i for i, (c, _) in enumerate(chunk_rows) if c == best), 0)
            start = full.find(chunk_rows[idx][1])
            lo = max(0, start - EXCERPT_CAP // 2)
            full = full[lo : lo + EXCERPT_CAP]
        hits.append(Hit(stype, sid, title, url, updated, bool(cidx), doc_scores[doc_id], full))
    return hits
```

- [ ] **Step 4: 전체 테스트 통과 확인** (인덱서 수정 포함 회귀)

Run: `pytest tests/ -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/search.py src/llmsearch/indexer.py tests/test_search.py
git commit -m "feat: 하이브리드 검색 — RRF/필터/최신성·Archive 랭킹/문서 승격"
```

---

### Task 8: 요약·분류 — Summarizer 인터페이스와 Gemini 구현

**Files:**
- Create: `src/llmsearch/summarize.py`
- Test: `tests/test_summarize.py`

**Interfaces:**
- Produces:
  - `@dataclass SummaryResult(markdown: str, category: str)` — `category`는 `"Projects/이름"` 형식 PARA 경로
  - `class Summarizer(Protocol):`
    - `summarize_and_classify(self, title: str, text: str, projects: list[str], areas: list[str], existing_resources: list[str], prior_category: str | None, glossary: str, rules: str) -> SummaryResult`
    - `describe_filename(self, filename: str) -> str` — DRM 폴백용 추정 설명
  - `FakeSummarizer` — 결정적: 첫 매칭 프로젝트명 포함 시 `Projects/이름`, 아니면 `Resources/일반`
  - `GeminiSummarizer(model: str)` — 요약 md에 `## 요약`, `## 예상 질문`(5개), `## 키워드` 섹션 강제(스펙 §7.1 P1), 분류는 닫힌 목록 우선(스펙 §7.1 P0). 응답 마지막 줄 `CATEGORY: <PARA경로>` 규약으로 파싱
  - `resolve_category(raw: str, projects, areas) -> str` — LLM 출력 검증: 닫힌 목록 밖의 Projects/Areas는 `Resources/`로 강등

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_summarize.py`

```python
from llmsearch.summarize import FakeSummarizer, resolve_category


def test_fake_summarizer_classifies_to_project():
    s = FakeSummarizer()
    r = s.summarize_and_classify(
        title="프로젝트A 킥오프.pptx", text="프로젝트A 일정 논의",
        projects=["프로젝트A"], areas=["팀운영"], existing_resources=[],
        prior_category=None, glossary="", rules="",
    )
    assert r.category == "Projects/프로젝트A"
    assert "## 요약" in r.markdown


def test_fake_summarizer_falls_back_to_resources():
    s = FakeSummarizer()
    r = s.summarize_and_classify("잡담.docx", "무관한 내용", ["프로젝트A"], [], [], None, "", "")
    assert r.category == "Resources/일반"


def test_fake_prior_category_sticky():
    s = FakeSummarizer()
    r = s.summarize_and_classify("x.pptx", "내용", [], [], [], "Areas/팀운영", "", "")
    assert r.category == "Areas/팀운영"  # 기존 분류 유지 우선 (분류 안정화)


def test_resolve_category_validates_closed_list():
    assert resolve_category("Projects/프로젝트A", ["프로젝트A"], []) == "Projects/프로젝트A"
    assert resolve_category("Projects/없는프젝", ["프로젝트A"], []) == "Resources/없는프젝"
    assert resolve_category("Resources/경쟁사", [], []) == "Resources/경쟁사"
    assert resolve_category("이상한값", [], []) == "Resources/일반"


def test_fake_describe_filename():
    d = FakeSummarizer().describe_filename("2026_상반기_실적보고_v3.pptx")
    assert "실적보고" in d
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_summarize.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/summarize.py`

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


@dataclass
class SummaryResult:
    markdown: str
    category: str  # PARA 경로: "Projects/x" | "Areas/x" | "Resources/x"


def resolve_category(raw: str, projects: list[str], areas: list[str]) -> str:
    """LLM 분류 출력을 검증한다: 닫힌 목록 밖의 Projects/Areas는 Resources로 강등 (스펙 §7.1 P0)."""
    raw = raw.strip().strip("/")
    parts = raw.split("/", 1)
    if len(parts) != 2 or not parts[1]:
        return "Resources/일반"
    top, name = parts
    if top == "Projects" and name in projects:
        return raw
    if top == "Areas" and name in areas:
        return raw
    if top in ("Projects", "Areas"):
        return f"Resources/{name}"
    if top in ("Resources", "Archives"):
        return raw
    return "Resources/일반"


class Summarizer(Protocol):
    def summarize_and_classify(
        self, title: str, text: str, projects: list[str], areas: list[str],
        existing_resources: list[str], prior_category: str | None, glossary: str, rules: str,
    ) -> SummaryResult: ...

    def describe_filename(self, filename: str) -> str: ...


class FakeSummarizer:
    """결정적 요약·분류 — 테스트용. 제목/본문에 프로젝트·영역명이 있으면 그리로 분류."""

    def summarize_and_classify(self, title, text, projects, areas, existing_resources,
                               prior_category, glossary, rules) -> SummaryResult:
        md = f"# {title}\n\n## 요약\n{text[:200]}\n\n## 예상 질문\n- {title}은 무엇인가?\n\n## 키워드\n{title}\n"
        if prior_category:
            return SummaryResult(md, prior_category)
        haystack = title + " " + text
        for p in projects:
            if p in haystack:
                return SummaryResult(md, f"Projects/{p}")
        for a in areas:
            if a in haystack:
                return SummaryResult(md, f"Areas/{a}")
        return SummaryResult(md, "Resources/일반")

    def describe_filename(self, filename: str) -> str:
        stem = filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")
        return f"파일명 기반 추정: {stem} 관련 문서 (내용 미인덱싱)"


_SUMMARY_PROMPT = """당신은 사내 문서 요약가다. 아래 문서를 검색에 최적화된 Markdown으로 요약하라.

반드시 이 구조를 따를 것:
# <문서 제목>
## 요약
(핵심 내용 5~10문장. 수치·날짜·고유명사 보존)
## 예상 질문
(이 문서로 답할 수 있는 질문 5개, 불릿)
## 키워드
(핵심 키워드·사람·프로젝트명, 쉼표 구분)

그리고 마지막 줄에 분류를 정확히 한 줄로 출력하라:
CATEGORY: <분류>

분류 규칙: 아래 활성 목록 중 가장 맞는 곳을 고른다. 어디에도 안 맞으면 Resources/<주제> 형식으로 새 주제를 만든다.
- 활성 프로젝트: {projects}
- 지속 영역(Areas): {areas}
- 기존 Resources 주제: {resources}
{prior}
{glossary}
{rules}

--- 문서 제목: {title} ---
{text}
"""


class GeminiSummarizer:
    def __init__(self, model: str = "gemini-flash-latest"):
        from google import genai  # 지연 import

        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = model

    def _generate(self, prompt: str) -> str:
        resp = self.client.models.generate_content(model=self.model, contents=prompt)
        return resp.text or ""

    def summarize_and_classify(self, title, text, projects, areas, existing_resources,
                               prior_category, glossary, rules) -> SummaryResult:
        prompt = _SUMMARY_PROMPT.format(
            projects=", ".join(projects) or "(없음)",
            areas=", ".join(areas) or "(없음)",
            resources=", ".join(existing_resources) or "(없음)",
            prior=f"- 이 문서의 기존 분류: {prior_category} (특별한 이유 없으면 유지)" if prior_category else "",
            glossary=f"\n## 용어집\n{glossary}" if glossary else "",
            rules=f"\n## 분류 규칙\n{rules}" if rules else "",
            title=title,
            text=text[:30000],  # 프롬프트 상한 — 초과분은 요약 대상에서 절단
        )
        out = self._generate(prompt)
        category = "Resources/일반"
        lines = out.strip().splitlines()
        for line in reversed(lines):
            if line.startswith("CATEGORY:"):
                category = resolve_category(line.removeprefix("CATEGORY:"), projects, areas)
                out = out[: out.rfind(line)].rstrip()
                break
        return SummaryResult(out, category)

    def describe_filename(self, filename: str) -> str:
        prompt = (
            "다음 파일명만 보고 이 문서가 무엇일지 2~3문장으로 추정 설명하라. "
            "검색 키워드가 될 고유명사를 보존하라.\n파일명: " + filename
        )
        return self._generate(prompt)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_summarize.py -v`
Expected: PASS (5건)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/summarize.py tests/test_summarize.py
git commit -m "feat: 요약·PARA 분류 — Summarizer 프로토콜, Fake/Gemini 구현"
```

---

### Task 9: notes 커넥터

**Files:**
- Create: `src/llmsearch/connectors/__init__.py`, `src/llmsearch/connectors/notes.py`
- Test: `tests/test_notes.py`

**Interfaces:**
- Consumes: `Document`, `SyncResult`, `rules.is_excluded`
- Produces: `sync_notes(folders: list[Path], excludes: list[str], state: dict) -> SyncResult`
  - `state` 형식: `{"files": {"<절대경로>": mtime(float)}}`. mtime 변화분만 documents로, 사라진 파일은 deleted_ids로

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_notes.py`

```python
import os
import time
from pathlib import Path

from llmsearch.connectors.notes import sync_notes


def test_initial_sync(tmp_path: Path):
    (tmp_path / "a.md").write_text("# 메모A\n내용", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("# 메모B", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("md 아님", encoding="utf-8")
    result = sync_notes([tmp_path], [], {})
    ids = {d.source_id for d in result.documents}
    assert len(ids) == 2 and all(i.endswith(".md") for i in ids)
    doc = next(d for d in result.documents if d.source_id.endswith("a.md"))
    assert doc.title == "메모A"  # 첫 헤딩을 제목으로
    assert doc.source_type == "notes"


def test_incremental_and_delete(tmp_path: Path):
    f = tmp_path / "a.md"
    f.write_text("v1", encoding="utf-8")
    r1 = sync_notes([tmp_path], [], {})
    # 변화 없음 → 빈 결과
    r2 = sync_notes([tmp_path], [], r1.state)
    assert r2.documents == [] and r2.deleted_ids == []
    # 수정 → 재수집
    os.utime(f, (time.time() + 10, time.time() + 10))
    r3 = sync_notes([tmp_path], [], r2.state)
    assert len(r3.documents) == 1
    # 삭제 → deleted_ids
    f.unlink()
    r4 = sync_notes([tmp_path], [], r3.state)
    assert len(r4.deleted_ids) == 1


def test_exclude(tmp_path: Path):
    (tmp_path / "비밀").mkdir()
    (tmp_path / "비밀" / "s.md").write_text("x", encoding="utf-8")
    result = sync_notes([tmp_path], ["path:**/비밀/**"], {})
    assert result.documents == []
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_notes.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/connectors/__init__.py` (빈 파일), `src/llmsearch/connectors/notes.py`

```python
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..models import Document, SyncResult
from ..rules import is_excluded


def _title_of(path: Path, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem


def sync_notes(folders: list[Path], excludes: list[str], state: dict) -> SyncResult:
    prev: dict[str, float] = dict(state.get("files", {}))
    seen: dict[str, float] = {}
    documents: list[Document] = []
    for folder in folders:
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.md")):
            sid = str(path.resolve())
            if is_excluded(sid, None, path.parent.name, excludes):
                continue
            mtime = path.stat().st_mtime
            seen[sid] = mtime
            if prev.get(sid) == mtime:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
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

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_notes.py -v`
Expected: PASS (3건)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/connectors tests/test_notes.py
git commit -m "feat: notes 커넥터 — md 폴더 증분 동기화/삭제 전파/제외 규칙"
```

---

### Task 10: local_docs 커넥터 — 추출, DRM 폴백, 요약·분류·복사

**Files:**
- Create: `src/llmsearch/connectors/local_docs.py`
- Test: `tests/test_local_docs.py`

**Interfaces:**
- Consumes: `Summarizer`, `rules.match_override/is_excluded`, `Document`, `SyncResult`
- Produces:
  - `sync_local_docs(folders, excludes, overrides, summarizer: Summarizer, summaries_dir: Path, projects, areas, glossary: str, class_rules: str, state: dict, prior_map: dict[str, tuple[str, str]]) -> SyncResult`
    - `state`: `{"files": {"<경로>": [mtime, size]}}`
    - `prior_map`: `source_id → (para_path, summary_path)` (호출자가 `indexer.get_para_map` 결과를 모아 전달; 반환 Document의 `extra`에 `para_path`, `summary_path` 포함 — 호출자가 `set_para_map` 기록)
  - `extract_text(path: Path) -> str` — markitdown 사용, 예외는 상위에서 DRM 판정
  - `looks_garbled(text: str) -> bool` — 유효문자 비율 < 0.6 또는 길이 < 50 → DRM 판정 (스펙 §7.1 P0)
  - 카테고리 변경 시 기존 요약본·복사본을 새 폴더로 **이동**(중복 생성 금지), DRM 문서는 `content_indexed=False` + 파일명 설명 인덱싱

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_local_docs.py`
  (markitdown 실변환 대신 텍스트 추출 함수를 monkeypatch — 커넥터 로직만 검증. `.docx` 실파일 테스트는 Step 4에서 1건)

```python
from pathlib import Path

import pytest
from llmsearch.connectors import local_docs
from llmsearch.summarize import FakeSummarizer


@pytest.fixture
def patch_extract(monkeypatch):
    def fake_extract(path: Path) -> str:
        if "drm" in path.name:
            raise RuntimeError("cannot open encrypted file")
        return f"{path.stem} 본문. 프로젝트A 관련 내용 " * 10
    monkeypatch.setattr(local_docs, "extract_text", fake_extract)


def run(tmp_path, docs_dir, state=None, prior=None):
    return local_docs.sync_local_docs(
        folders=[docs_dir], excludes=[], overrides=[],
        summarizer=FakeSummarizer(), summaries_dir=tmp_path / "summaries",
        projects=["프로젝트A"], areas=[], glossary="", class_rules="",
        state=state or {}, prior_map=prior or {},
    )


def test_summarize_classify_copy(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "킥오프.pptx").write_bytes(b"fake-pptx")
    result = run(tmp_path, docs)
    assert len(result.documents) == 1
    d = result.documents[0]
    assert d.extra["para_path"] == "Projects/프로젝트A"
    summary = Path(d.extra["summary_path"])
    assert summary.exists() and summary.suffix == ".md"
    assert (summary.parent / "킥오프.pptx").exists()  # 원본 복사 (스펙 §7.1)
    assert "## 요약" in d.text


def test_drm_fallback(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "drm_실적보고.pptx").write_bytes(b"encrypted")
    result = run(tmp_path, docs)
    d = result.documents[0]
    assert d.content_indexed is False
    assert "실적보고" in d.text  # 파일명 기반 설명


def test_category_move_no_duplicate(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    f = docs / "킥오프.pptx"; f.write_bytes(b"v1")
    r1 = run(tmp_path, docs)
    old_summary = Path(r1.documents[0].extra["summary_path"])
    # 재요약 시 분류가 바뀌는 상황을 prior_map 없이 강제: prior를 다른 카테고리로 주면 유지되므로,
    # 여기서는 prior가 Resources였다가 이번에 Projects로 가는 케이스 대신
    # 같은 파일을 수정해 prior=Projects 유지 + 파일 갱신 → 이동 없음/중복 없음 확인
    import os, time
    os.utime(f, (time.time() + 5, time.time() + 5))
    r2 = run(tmp_path, docs, state=r1.state,
             prior={r1.documents[0].source_id: ("Projects/프로젝트A", str(old_summary))})
    assert Path(r2.documents[0].extra["summary_path"]).exists()
    # summaries 아래에 같은 원본 복사본이 1개만 존재
    copies = list((tmp_path / "summaries").rglob("킥오프.pptx"))
    assert len(copies) == 1


def test_deleted_file_cleans_copies(tmp_path: Path, patch_extract):
    docs = tmp_path / "docs"; docs.mkdir()
    f = docs / "킥오프.pptx"; f.write_bytes(b"v1")
    r1 = run(tmp_path, docs)
    sid = r1.documents[0].source_id
    summary = Path(r1.documents[0].extra["summary_path"])
    f.unlink()
    r2 = run(tmp_path, docs, state=r1.state, prior={sid: ("Projects/프로젝트A", str(summary))})
    assert r2.deleted_ids == [sid]
    assert not summary.exists()  # 요약본·복사본 정리 (스펙 §6 삭제 전파)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_local_docs.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/connectors/local_docs.py`

```python
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from ..models import Document, SyncResult
from ..rules import is_excluded, match_override
from ..summarize import Summarizer

EXTENSIONS = {".pptx", ".xlsx", ".docx", ".pdf"}
MIN_TEXT_LEN = 50
VALID_RATIO = 0.6


def extract_text(path: Path) -> str:
    from markitdown import MarkItDown  # 지연 import — 무거운 의존성

    return MarkItDown().convert(str(path)).text_content or ""


def looks_garbled(text: str) -> bool:
    """DRM/암호화 문서 판정: 추출 텍스트가 너무 짧거나 유효 문자 비율이 낮다 (스펙 §7.1 P0)."""
    stripped = text.strip()
    if len(stripped) < MIN_TEXT_LEN:
        return True
    valid = sum(1 for ch in stripped if ch.isalnum() or ch.isspace() or ch in ".,;:!?()[]{}#*-_/%~'\"")
    return valid / len(stripped) < VALID_RATIO


def _existing_resources(summaries_dir: Path) -> list[str]:
    res = summaries_dir / "Resources"
    return sorted(p.name for p in res.iterdir() if p.is_dir()) if res.exists() else []


def _place(summaries_dir: Path, category: str, original: Path, summary_md: str,
           prior: tuple[str, str] | None) -> str:
    """요약 md와 원본 복사본을 카테고리 폴더에 기록. 카테고리 변경 시 이전 것을 이동(삭제 후 재생성)."""
    target_dir = summaries_dir / category
    target_dir.mkdir(parents=True, exist_ok=True)
    if prior:
        old_summary = Path(prior[1])
        if old_summary.exists() and old_summary.parent != target_dir:
            old_summary.unlink()
            old_copy = old_summary.parent / original.name
            if old_copy.exists():
                old_copy.unlink()
    summary_path = target_dir / (original.name + ".md")
    summary_path.write_text(summary_md, encoding="utf-8")
    copy_path = target_dir / original.name
    if not copy_path.exists() or copy_path.stat().st_mtime < original.stat().st_mtime:
        shutil.copy2(original, copy_path)
    return str(summary_path)


def _cleanup(prior: tuple[str, str] | None) -> None:
    if not prior:
        return
    summary = Path(prior[1])
    if summary.exists():
        summary.unlink()
    stem = summary.name.removesuffix(".md")
    copy = summary.parent / stem
    if copy.exists():
        copy.unlink()


def sync_local_docs(
    folders: list[Path], excludes: list[str], overrides: list[dict],
    summarizer: Summarizer, summaries_dir: Path,
    projects: list[str], areas: list[str], glossary: str, class_rules: str,
    state: dict, prior_map: dict[str, tuple[str, str]],
) -> SyncResult:
    prev: dict[str, list] = dict(state.get("files", {}))
    seen: dict[str, list] = {}
    documents: list[Document] = []

    for folder in folders:
        if not folder.exists():
            continue
        for path in sorted(p for p in folder.rglob("*") if p.suffix.lower() in EXTENSIONS):
            sid = str(path.resolve())
            if is_excluded(sid, None, path.parent.name, excludes):
                continue
            st = path.stat()
            sig = [st.st_mtime, st.st_size]
            seen[sid] = sig
            if prev.get(sid) == sig:
                continue

            prior = prior_map.get(sid)
            content_indexed = True
            try:
                text = extract_text(path)
                if looks_garbled(text):
                    raise ValueError("garbled")
            except Exception:
                content_indexed = False
                text = ""

            if content_indexed:
                result = summarizer.summarize_and_classify(
                    title=path.name, text=text, projects=projects, areas=areas,
                    existing_resources=_existing_resources(summaries_dir),
                    prior_category=prior[0] if prior else None,
                    glossary=glossary, rules=class_rules,
                )
                category, body = result.category, result.markdown
            else:
                # DRM 폴백: 파일명·메타데이터만으로 설명 생성 (스펙 §7.1 P0)
                desc = summarizer.describe_filename(path.name)
                override = match_override(sid, None, overrides)
                category = override or (prior[0] if prior else "Resources/미분류")
                body = (
                    f"# {path.name}\n\n## 요약\n{desc}\n\n"
                    f"(🔒 DRM/암호화로 내용 미인덱싱 — 파일명 기반)\n\n"
                    f"## 키워드\n{path.stem.replace('_', ' ').replace('-', ' ')}\n"
                )
            override = match_override(sid, None, overrides)
            if override:
                category = override  # 결정적 규칙이 LLM 판단보다 우선 (스펙 §9)

            summary_path = _place(summaries_dir, category, path, body, prior)
            documents.append(
                Document(
                    source_type="local_docs", source_id=sid, title=path.name,
                    text=body, url_or_path=sid,
                    updated_at=datetime.fromtimestamp(st.st_mtime),
                    content_indexed=content_indexed,
                    extra={"para_path": category, "summary_path": summary_path},
                )
            )

    deleted = [sid for sid in prev if sid not in seen]
    for sid in deleted:
        _cleanup(prior_map.get(sid))
    return SyncResult(documents=documents, deleted_ids=deleted, state={"files": seen})
```

- [ ] **Step 4: 테스트 통과 확인 + markitdown 실변환 스모크 1건 추가**

Run: `pytest tests/test_local_docs.py -v`
Expected: PASS (4건)

`tests/test_local_docs.py`에 추가 (실제 docx 생성이 어려우므로 markitdown이 지원하는 최소 포맷으로 — pdf/docx 픽스처가 없으면 skip):

```python
def test_extract_text_real_smoke(tmp_path: Path):
    """markitdown 실변환 스모크 — 지원 포맷 파일이 없으면 skip."""
    pytest.importorskip("markitdown")
    # 텍스트 파일은 EXTENSIONS 밖이므로 변환기 직접 호출만 확인
    f = tmp_path / "t.txt"
    f.write_text("스모크 텍스트", encoding="utf-8")
    from llmsearch.connectors.local_docs import extract_text
    assert "스모크" in extract_text(f)
```

Run: `pytest tests/test_local_docs.py -v`
Expected: PASS (5건)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/connectors/local_docs.py tests/test_local_docs.py
git commit -m "feat: local_docs 커넥터 — 추출/DRM 폴백/PARA 분류·복사/삭제 정리"
```

---

### Task 11: Claude 에이전틱 답변 루프

**Files:**
- Create: `src/llmsearch/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Hit`, `search`의 시그니처 (`search_fn(query, source_filter, date_from, date_to, sender) -> list[Hit]` 형태로 주입)
- Produces:
  - `class Answerer(Protocol): def answer_stream(self, question: str, history: list[dict], search_fn) -> Iterator[dict]`
    - 이벤트: `{"type": "text", "text": str}` (스트리밍 텍스트 조각), `{"type": "sources", "hits": list[Hit]}` (사용된 전체 출처, 종료 직전 1회), `{"type": "error", "message": str}`
  - `FakeAnswerer` — search_fn 1회 호출 후 첫 Hit 제목을 인용한 고정 답변
  - `ClaudeAnswerer(model: str, active_projects: list[str], answer_rules: str, glossary: str)` — 수동 툴 루프: 사전 검색 1회 결과 + `search` 툴 제공, 추가 툴 호출 최대 3회, `client.messages.stream` 스트리밍, `stop_reason == "refusal"` 처리, 시스템 프롬프트에 현재 날짜·활성 프로젝트·근거 없는 내용 금지·출처 번호 표기 규칙 주입 (스펙 §8)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_llm.py`

```python
from datetime import datetime

from llmsearch.llm import FakeAnswerer
from llmsearch.models import Hit

HIT = Hit("notes", "a.md", "프로젝트A 킥오프", "/n/a.md", "2026-08-01", True, 1.0, "회의록 본문")


def test_fake_answerer_streams_and_cites():
    calls = []

    def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
        calls.append(query)
        return [HIT]

    events = list(FakeAnswerer().answer_stream("킥오프 언제였지?", [], search_fn))
    assert calls == ["킥오프 언제였지?"]
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "프로젝트A 킥오프" in text
    sources = [e for e in events if e["type"] == "sources"]
    assert len(sources) == 1 and sources[0]["hits"][0].source_id == "a.md"


def test_fake_answerer_no_results():
    events = list(FakeAnswerer().answer_stream("없는 질문", [], lambda *a, **k: []))
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "찾지 못" in text


def test_claude_answerer_importable_without_key():
    # 키 없이 모듈 import 가능해야 함 (지연 초기화)
    from llmsearch.llm import ClaudeAnswerer  # noqa: F401
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/llm.py`

```python
from __future__ import annotations

import json
from datetime import date
from typing import Callable, Iterator, Protocol

from .models import Hit

SearchFn = Callable[..., list[Hit]]
MAX_TOOL_ROUNDS = 3  # 사전 검색 이후 Claude 추가 검색 상한 (스펙 §8)

_SEARCH_TOOL = {
    "name": "search",
    "description": (
        "사내 통합 인덱스(로컬 문서 요약, 개인 메모)를 검색한다. "
        "첫 검색 결과가 부족하거나, 다른 표현·필터로 더 찾아야 할 때 호출하라. "
        "일정·날짜 관련 질문은 date_from/date_to 필터를 사용하라."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "검색 질의"},
            "source_filter": {"type": "array", "items": {"type": "string",
                "enum": ["notes", "local_docs"]}, "description": "소스 한정(선택)"},
            "date_from": {"type": "string", "description": "YYYY-MM-DD 이후(선택)"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD 이전(선택)"},
        },
        "required": ["query"],
    },
}


class Answerer(Protocol):
    def answer_stream(self, question: str, history: list[dict], search_fn: SearchFn) -> Iterator[dict]: ...


class FakeAnswerer:
    def answer_stream(self, question, history, search_fn) -> Iterator[dict]:
        hits = search_fn(question)
        if not hits:
            yield {"type": "text", "text": "관련 문서를 찾지 못했습니다."}
            yield {"type": "sources", "hits": []}
            return
        yield {"type": "text", "text": f"[1] {hits[0].title} 문서에 따르면: {hits[0].excerpt[:100]}"}
        yield {"type": "sources", "hits": hits}


def _hits_block(hits: list[Hit], offset: int = 0) -> str:
    lines = []
    for i, h in enumerate(hits, start=offset + 1):
        drm = " (🔒 내용 미인덱싱 — 파일명 기반 매칭)" if not h.content_indexed else ""
        lines.append(f"[{i}] {h.title} | {h.source_type} | {h.updated_at}{drm}\n{h.excerpt}\n")
    return "\n".join(lines) or "(검색 결과 없음)"


class ClaudeAnswerer:
    def __init__(self, model: str = "claude-opus-5", active_projects: list[str] | None = None,
                 answer_rules: str = "", glossary: str = ""):
        import anthropic  # 지연 import

        self.client = anthropic.Anthropic()
        self.model = model
        self.active_projects = active_projects or []
        self.answer_rules = answer_rules
        self.glossary = glossary

    def _system(self) -> str:
        parts = [
            "당신은 개인용 사내 문서 검색 비서다.",
            f"오늘 날짜: {date.today().isoformat()}",
            "규칙: 제공된 근거 문서에 없는 내용은 지어내지 말 것. 각 주장 끝에 [번호] 출처를 표기할 것. "
            "근거가 부족하면 부족하다고 말할 것. 답은 한국어, 두괄식.",
        ]
        if self.active_projects:
            parts.append("현재 진행 중 프로젝트(관련 문서 우선 판단): " + ", ".join(self.active_projects))
        if self.glossary:
            parts.append("## 용어집\n" + self.glossary)
        if self.answer_rules:
            parts.append("## 답변 규칙\n" + self.answer_rules)
        return "\n\n".join(parts)

    def answer_stream(self, question, history, search_fn) -> Iterator[dict]:
        all_hits: list[Hit] = list(search_fn(question))  # fast path 사전 검색 (스펙 §8)
        messages = list(history) + [{
            "role": "user",
            "content": f"질문: {question}\n\n사전 검색 결과:\n{_hits_block(all_hits)}",
        }]
        rounds = 0
        try:
            while True:
                with self.client.messages.stream(
                    model=self.model, max_tokens=16000,
                    system=self._system(), tools=[_SEARCH_TOOL], messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        yield {"type": "text", "text": text}
                    response = stream.get_final_message()

                if response.stop_reason == "refusal":
                    yield {"type": "error", "message": "안전 정책으로 답변이 거부되었습니다."}
                    break
                if response.stop_reason != "tool_use" or rounds >= MAX_TOOL_ROUNDS:
                    break

                rounds += 1
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    args = block.input if isinstance(block.input, dict) else json.loads(block.input)
                    hits = search_fn(
                        args.get("query", question),
                        source_filter=args.get("source_filter"),
                        date_from=args.get("date_from"),
                        date_to=args.get("date_to"),
                    )
                    known = {h.source_id for h in all_hits}
                    all_hits.extend(h for h in hits if h.source_id not in known)
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": _hits_block(hits, offset=len(all_hits) - len(hits)),
                    })
                messages.append({"role": "user", "content": tool_results})
        except Exception as exc:  # API 실패 시에도 출처는 전달 (스펙 §5 에러 처리)
            yield {"type": "error", "message": f"답변 생성 실패: {exc}"}
        yield {"type": "sources", "hits": all_hits}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_llm.py -v`
Expected: PASS (3건)

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/llm.py tests/test_llm.py
git commit -m "feat: Claude 에이전틱 답변 루프(search 툴 최대 3회, 스트리밍, refusal 처리)"
```

---

### Task 12: FastAPI 웹앱 — 채팅 SSE, 소스 상태, 수동 동기화, 스케줄러, UI

**Files:**
- Create: `src/llmsearch/web/__init__.py`, `src/llmsearch/web/app.py`, `src/llmsearch/web/static/index.html`, `src/llmsearch/__main__.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: 전 계층 (여기서만 조립)
- Produces:
  - `create_app(config: Config, embedder=None, summarizer=None, answerer=None) -> FastAPI` — None이면 실 구현(Gemini/Claude), 테스트는 Fake 주입
  - 라우트: `GET /` (index.html), `POST /api/chat` (`{"question": str, "history": []}` → SSE: `text`/`sources`/`error`/`done` 이벤트), `GET /api/sources` (소스별 문서 수·마지막 동기화·오류), `POST /api/sync/{source}` (수동 동기화, `notes`|`local_docs`), `GET /api/log` (최근 동기화 로그 목록)
  - `run_sync(app_state, source: str) -> dict` — 커넥터 실행→인덱서 반영→sync_state·para_map 기록·로그 적재. 실패는 소스별 격리(예외를 로그로)
  - 스케줄러: FastAPI lifespan에서 `asyncio.create_task`로 주기 실행 (`config.sync_interval_minutes`), 테스트에서는 비활성(`enable_scheduler=False`)
  - `python -m llmsearch --config config.yaml` 진입점 (uvicorn, host 127.0.0.1 고정)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_web.py`

```python
import json
from pathlib import Path

from fastapi.testclient import TestClient

from llmsearch.config import Config
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.llm import FakeAnswerer
from llmsearch.summarize import FakeSummarizer
from llmsearch.web.app import create_app


def make_app(tmp_path: Path) -> TestClient:
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "kick.md").write_text("# 프로젝트A 킥오프\n8월 1일 진행", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", notes_folders=[notes], projects=["프로젝트A"])
    app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                     answerer=FakeAnswerer(), enable_scheduler=False)
    return TestClient(app)


def test_index_page(tmp_path: Path):
    client = make_app(tmp_path)
    r = client.get("/")
    assert r.status_code == 200 and "llmsearch" in r.text


def test_manual_sync_and_sources(tmp_path: Path):
    client = make_app(tmp_path)
    r = client.post("/api/sync/notes")
    assert r.status_code == 200
    assert r.json()["indexed"] == 1
    r = client.get("/api/sources")
    notes_status = next(s for s in r.json() if s["source"] == "notes")
    assert notes_status["doc_count"] == 1
    assert notes_status["last_sync"] is not None


def test_chat_sse(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    with client.stream("POST", "/api/chat", json={"question": "킥오프 언제?", "history": []}) as r:
        body = "".join(r.iter_text())
    assert "event: sources" in body
    assert "event: done" in body
    assert "킥오프" in body


def test_sync_unknown_source(tmp_path: Path):
    client = make_app(tmp_path)
    assert client.post("/api/sync/outlook").status_code == 404


def test_sync_log(tmp_path: Path):
    client = make_app(tmp_path)
    client.post("/api/sync/notes")
    log = client.get("/api/log").json()
    assert log and log[0]["source"] == "notes" and log[0]["ok"] is True
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_web.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/web/app.py`

```python
from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from .. import db, indexer, search
from ..config import Config
from ..connectors.local_docs import sync_local_docs
from ..connectors.notes import sync_notes
from ..rules import load_rules_md

STATIC_DIR = Path(__file__).parent / "static"
SOURCES = ("notes", "local_docs")


def run_sync(state: dict, source: str) -> dict:
    """커넥터 1개 동기화 실행. 실패는 소스별로 격리해 로그에 남긴다 (스펙 §5)."""
    cfg: Config = state["config"]
    conn = state["conn"]
    entry = {"source": source, "at": datetime.now().isoformat(), "ok": True, "indexed": 0,
             "deleted": 0, "error": None}
    try:
        prev = indexer.get_sync_state(conn, source)
        rules_md = load_rules_md(cfg.rules_md_path)
        if source == "notes":
            result = sync_notes(cfg.notes_folders, cfg.exclude, prev)
        else:  # local_docs
            prior_map = {
                sid: pm for sid in list(prev.get("files", {}))
                if (pm := indexer.get_para_map(conn, sid))
            }
            result = sync_local_docs(
                folders=cfg.watch_folders, excludes=cfg.exclude, overrides=cfg.para_overrides,
                summarizer=state["summarizer"], summaries_dir=cfg.summaries_dir,
                projects=cfg.projects, areas=cfg.areas,
                glossary=rules_md.get("용어집", ""), class_rules=rules_md.get("분류 규칙", ""),
                state=prev, prior_map=prior_map,
            )
        entry["indexed"] = indexer.index_documents(conn, result.documents, state["embedder"])
        entry["deleted"] = indexer.delete_documents(conn, source, result.deleted_ids)
        for doc in result.documents:
            if "summary_path" in doc.extra:
                indexer.set_para_map(conn, doc.source_id, doc.extra["para_path"], doc.extra["summary_path"])
        indexer.set_sync_state(conn, source, result.state)
    except Exception as exc:
        entry["ok"] = False
        entry["error"] = f"{exc}\n{traceback.format_exc(limit=3)}"
    state["log"].insert(0, entry)
    del state["log"][200:]
    return entry


def create_app(config: Config, embedder=None, summarizer=None, answerer=None,
               enable_scheduler: bool = True) -> FastAPI:
    if embedder is None:
        from ..embeddings import GeminiEmbeddings
        embedder = GeminiEmbeddings(model=config.embed_model)
    if summarizer is None:
        from ..summarize import GeminiSummarizer
        summarizer = GeminiSummarizer(model=config.summary_model)
    if answerer is None:
        from ..llm import ClaudeAnswerer
        rules_md = load_rules_md(config.rules_md_path)
        answerer = ClaudeAnswerer(
            model=config.answer_model, active_projects=config.projects,
            answer_rules=rules_md.get("답변 규칙", ""), glossary=rules_md.get("용어집", ""),
        )

    conn = db.open_db(config.db_path)
    state = {"config": config, "conn": conn, "embedder": embedder,
             "summarizer": summarizer, "answerer": answerer, "log": []}

    app = FastAPI(title="llmsearch")
    app.state.llmsearch = state

    async def scheduler_loop():
        while True:
            await asyncio.sleep(config.sync_interval_minutes * 60)
            for source in SOURCES:
                await asyncio.to_thread(run_sync, state, source)

    @app.on_event("startup")
    async def _startup():
        if enable_scheduler:
            state["scheduler"] = asyncio.create_task(scheduler_loop())

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/sources")
    def sources():
        out = []
        for source in SOURCES:
            row = conn.execute("SELECT COUNT(*) FROM documents WHERE source_type=?", (source,)).fetchone()
            last = next((e for e in state["log"] if e["source"] == source), None)
            out.append({"source": source, "doc_count": row[0],
                        "last_sync": last["at"] if last else None,
                        "last_error": last["error"] if last else None})
        return out

    @app.post("/api/sync/{source}")
    def manual_sync(source: str):
        if source not in SOURCES:
            raise HTTPException(404, f"unknown source: {source}")
        return run_sync(state, source)

    @app.get("/api/log")
    def log():
        return state["log"]

    @app.post("/api/chat")
    def chat(payload: dict):
        question = payload.get("question", "")
        history = payload.get("history", [])

        def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
            return search.search(conn, embedder, query, source_filter=source_filter,
                                 date_from=date_from, date_to=date_to, sender=sender)

        def event_stream():
            for ev in state["answerer"].answer_stream(question, history, search_fn):
                if ev["type"] == "sources":
                    data = json.dumps([asdict(h) for h in ev["hits"]], ensure_ascii=False)
                    yield f"event: sources\ndata: {data}\n\n"
                elif ev["type"] == "error":
                    yield f"event: error\ndata: {json.dumps(ev['message'], ensure_ascii=False)}\n\n"
                else:
                    yield f"event: text\ndata: {json.dumps(ev['text'], ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app
```

`src/llmsearch/__main__.py`:

```python
import argparse
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .config import load_config
from .web.app import create_app


def main():
    parser = argparse.ArgumentParser(prog="llmsearch")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8642)
    args = parser.parse_args()
    load_dotenv()
    app = create_app(load_config(args.config))
    uvicorn.run(app, host="127.0.0.1", port=args.port)  # 로컬 전용 (스펙 §10)


if __name__ == "__main__":
    main()
```

`src/llmsearch/web/static/index.html` (경량 단일 페이지 — 탭 3개, SSE 수신):

```html
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>llmsearch</title>
<style>
  body { font-family: sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  nav button { margin-right: .5rem; padding: .4rem .8rem; cursor: pointer; }
  .tab { display: none; } .tab.active { display: block; }
  #messages { border: 1px solid #ccc; min-height: 300px; padding: 1rem; margin: 1rem 0;
              white-space: pre-wrap; overflow-y: auto; max-height: 60vh; }
  .msg-q { font-weight: bold; margin-top: 1rem; }
  .src { background: #f4f4f4; border-radius: 6px; padding: .5rem; margin: .3rem 0; font-size: .9rem; }
  #question { width: 80%; padding: .5rem; } table { border-collapse: collapse; }
  td, th { border: 1px solid #ccc; padding: .4rem .8rem; }
</style>
</head>
<body>
<h1>llmsearch</h1>
<nav>
  <button onclick="show('chat')">채팅</button>
  <button onclick="show('sources')">소스</button>
  <button onclick="show('log')">로그</button>
</nav>

<div id="chat" class="tab active">
  <div id="messages"></div>
  <form onsubmit="ask(event)">
    <input id="question" placeholder="질문을 입력하세요" autocomplete="off">
    <button>검색</button>
  </form>
</div>

<div id="sources" class="tab">
  <table id="srcTable"><thead><tr><th>소스</th><th>문서 수</th><th>마지막 동기화</th><th></th></tr></thead>
  <tbody></tbody></table>
</div>

<div id="log" class="tab"><pre id="logBody"></pre></div>

<script>
const history = [];
function show(id) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (id === 'sources') loadSources();
  if (id === 'log') loadLog();
}
async function loadSources() {
  const data = await (await fetch('/api/sources')).json();
  document.querySelector('#srcTable tbody').innerHTML = data.map(s =>
    `<tr><td>${s.source}</td><td>${s.doc_count}</td><td>${s.last_sync ?? '-'}` +
    `${s.last_error ? ' ⚠️' : ''}</td>` +
    `<td><button onclick="syncNow('${s.source}')">동기화</button></td></tr>`).join('');
}
async function syncNow(source) {
  await fetch('/api/sync/' + source, {method: 'POST'});
  loadSources();
}
async function loadLog() {
  const data = await (await fetch('/api/log')).json();
  document.getElementById('logBody').textContent = JSON.stringify(data, null, 2);
}
async function ask(e) {
  e.preventDefault();
  const q = document.getElementById('question').value.trim();
  if (!q) return;
  document.getElementById('question').value = '';
  const box = document.getElementById('messages');
  box.innerHTML += `<div class="msg-q">Q. ${q}</div><div class="msg-a"></div>`;
  const answerDiv = box.lastElementChild;
  const resp = await fetch('/api/chat', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({question: q, history}),
  });
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '', answerText = '';
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    buf += dec.decode(value, {stream: true});
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
      const ev = (block.match(/^event: (.+)$/m) || [])[1];
      const data = (block.match(/^data: (.+)$/m) || [])[1];
      if (ev === 'text') { answerText += JSON.parse(data); answerDiv.textContent = answerText; }
      else if (ev === 'error') { answerDiv.textContent += '\n⚠️ ' + JSON.parse(data); }
      else if (ev === 'sources') {
        for (const h of JSON.parse(data)) {
          const lock = h.content_indexed ? '' : ' 🔒';
          answerDiv.insertAdjacentHTML('beforeend',
            `<div class="src">📄 ${h.title}${lock} <small>(${h.source_type} · ${h.updated_at})</small><br>` +
            `<code>${h.url_or_path}</code></div>`);
        }
      }
      box.scrollTop = box.scrollHeight;
    }
  }
  history.push({role: 'user', content: q}, {role: 'assistant', content: answerText});
}
</script>
</body>
</html>
```

- [ ] **Step 4: 테스트 통과 확인 (전체 회귀 포함)**

Run: `pytest tests/ -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/web src/llmsearch/__main__.py tests/test_web.py
git commit -m "feat: FastAPI 웹앱 — 채팅 SSE/소스 상태/수동 동기화/스케줄러/UI"
```

---

### Task 13: 골든 평가 스크립트 + 실행 문서

**Files:**
- Create: `src/llmsearch/eval/__init__.py`, `src/llmsearch/eval/golden.py`, `README.md`, `config.example.yaml`, `.env.example`
- Test: `tests/test_golden.py`

**Interfaces:**
- Consumes: `search.search`, `db.open_db`, `Config`
- Produces:
  - `evaluate(conn, embedder, cases: list[dict]) -> dict` — `cases` = `[{"question": str, "expect_source_id": str}]`, 반환 `{"total", "hit_at_3", "rate", "misses": [...]}`
  - CLI: `python -m llmsearch.eval.golden --config config.yaml --golden golden.yaml` (스펙 §1 성공 기준 측정: 상위 3위 적중률 ≥ 70%)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_golden.py`

```python
from datetime import datetime
from pathlib import Path

from llmsearch import db, indexer
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.eval.golden import evaluate
from llmsearch.models import Document


def test_evaluate(tmp_path: Path):
    conn = db.open_db(tmp_path / "g.db")
    emb = FakeEmbeddings(dim=768)
    indexer.index_documents(conn, [
        Document("notes", "kick.md", "프로젝트A 킥오프", "프로젝트A 킥오프 회의록 8월 1일",
                 "/n/kick.md", datetime(2026, 8, 1)),
        Document("notes", "lunch.md", "점심", "김치찌개", "/n/lunch.md", datetime(2026, 8, 1)),
    ], emb)
    report = evaluate(conn, emb, [
        {"question": "프로젝트A 킥오프 언제?", "expect_source_id": "kick.md"},
        {"question": "존재하지 않는 주제 XYZQW", "expect_source_id": "none.md"},
    ])
    assert report["total"] == 2
    assert report["hit_at_3"] == 1
    assert report["rate"] == 0.5
    assert report["misses"][0]["question"].startswith("존재하지")
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_golden.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `src/llmsearch/eval/golden.py`

```python
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .. import db, search
from ..config import load_config


def evaluate(conn, embedder, cases: list[dict]) -> dict:
    hits_at_3 = 0
    misses = []
    for case in cases:
        results = search.search(conn, embedder, case["question"], k=3)
        found = [h.source_id for h in results]
        if any(case["expect_source_id"] in sid for sid in found):
            hits_at_3 += 1
        else:
            misses.append({"question": case["question"], "expected": case["expect_source_id"], "got": found})
    total = len(cases)
    return {"total": total, "hit_at_3": hits_at_3,
            "rate": hits_at_3 / total if total else 0.0, "misses": misses}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--golden", type=Path, required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    from ..embeddings import GeminiEmbeddings

    conn = db.open_db(cfg.db_path)
    cases = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
    report = evaluate(conn, GeminiEmbeddings(model=cfg.embed_model), cases)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    target = 0.7  # 스펙 §1 성공 기준
    print(f"\n상위3 적중률 {report['rate']:.0%} (목표 {target:.0%}) -> {'PASS' if report['rate'] >= target else 'FAIL'}")


if __name__ == "__main__":
    main()
```

`config.example.yaml`:

```yaml
data_dir: "D:/llmsearch-data"        # WSL 개발 시 /mnt/d/llmsearch-data
watch_folders: ["D:/업무문서"]        # local_docs 감시 폴더
notes_folders: ["D:/llmsearch-data/notes"]
para:
  projects: ["프로젝트A"]             # 활성 프로젝트 — 닫힌 목록 (GUI 개편 전까지 여기서 관리)
  areas: ["팀운영"]
rules:
  para_overrides: []                  # 예: - {match: "path:**/경영회의/**", target: "Areas/경영지원"}
  exclude: []                         # 예: - "folder:인사평가"
sync_interval_minutes: 30
```

`.env.example`:

```
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

`README.md`:

```markdown
# llmsearch

개인용 통합 문서 검색 툴. 스펙: `docs/superpowers/specs/2026-08-17-llmsearch-design.md`

## 설치 (Windows Python 기준)
1. `pip install -e ".[vec]"` (sqlite-vec 실패 시 `pip install -e .` — numpy 폴백 자동)
2. `python scripts/spike_sqlite_vec.py` 로 벡터 확장 동작 확인
3. `config.example.yaml` → `config.yaml` 복사 후 경로 수정
4. `.env.example` → `.env` 복사 후 API 키 기입 (Gemini는 **유료 티어 필수** — 무료 티어는 입력이 학습에 사용됨)

## 실행
`python -m llmsearch --config config.yaml` → http://127.0.0.1:8642

## 평가
`golden.yaml`에 `[{question, expect_source_id}]` 작성 후:
`python -m llmsearch.eval.golden --config config.yaml --golden golden.yaml`

## 개발
- 테스트: `pytest` (WSL에서 실행 가능, API 키 불필요)
- 인덱스는 소모품: 스키마 변경·손상 시 `index.db` 삭제 후 재동기화
```

- [ ] **Step 4: 테스트 통과 확인 (최종 전체 회귀)**

Run: `pytest tests/ -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llmsearch/eval tests/test_golden.py README.md config.example.yaml .env.example
git commit -m "feat: 골든 평가 스크립트 + 실행 문서(README/설정 예시)"
```

---

## M1 완료 기준 (수동 검증 체크리스트)

자동 테스트 외에 Windows 실환경에서 1회 확인:

1. `python scripts/spike_sqlite_vec.py` — sqlite-vec 동작 여부 확인 (스펙 §11 리스크)
2. 실제 `.env` 키로 앱 실행 → notes 폴더에 md 2~3개 넣고 소스 탭에서 동기화 → 채팅 질문에 출처 카드 표시 확인
3. 감시 폴더에 실제 pptx 1개 → `summaries/<PARA>/` 아래 요약 md + 원본 복사 생성 확인
4. DRM 걸린 실제 파일 1개 → 🔒 배지로 검색되는지 확인
5. 골든 질문 10개 작성 → 평가 스크립트 실행, 적중률 기록 (이후 튜닝의 기준선)
```
