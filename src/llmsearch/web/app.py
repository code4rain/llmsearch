from __future__ import annotations

import asyncio
import json
import logging
import threading
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import db, indexer, search
from ..archive import archive_project
from ..atlassian.auth import diagnose, resolve_auth_candidates
from ..atlassian.registry import Registry
from ..config import Config
from ..connectors.confluence import sync_confluence
from ..connectors.jira import sync_jira
from ..connectors.local_docs import sync_local_docs
from ..connectors.notes import sync_notes
from ..connectors.outlook_cal import sync_outlook_cal
from ..connectors.outlook_mail import backlog_hint, sync_outlook_mail
from ..rules import load_rules_md
from ..usage import CountingEmbedder, CountingSummarizer, UsageTracker

STATIC_DIR = Path(__file__).parent / "static"
SOURCES = ("notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira")
_AUTH_EXPIRED_MSG = (
    "Atlassian 인증이 만료되었습니다. .env의 자격증명(ATLASSIAN_* 또는 서비스별 "
    "CONFLUENCE_*/JIRA_*)을 갱신한 뒤 다시 동기화하세요."
)
_logger = logging.getLogger(__name__)


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


def _scheduled_sources(state: dict) -> list[str]:
    """스케줄러가 이번 라운드에 실제로 동기화할 소스 목록.

    confluence/jira는 atlassian registry에 등록된 게 하나도 없으면 매 라운드(기본
    30분)마다 의미 없는 오류 로그만 쌓이므로 스킵한다. 수동 동기화(/api/sync/{source})는
    이 필터를 거치지 않는다 — 등록 직후 사용자가 바로 확인해 보는 경우를 막지 않기 위해서다.
    """
    registry = state["registry"]
    out = []
    for source in SOURCES:
        if source == "confluence" and not registry.confluence_page_ids():
            continue
        if source == "jira" and not registry.jira_keys():
            continue
        out.append(source)
    return out


def run_sync(state: dict, source: str) -> dict:
    """커넥터 1개 동기화 실행. 실패는 소스별로 격리해 로그에 남긴다 (스펙 §5)."""
    cfg: Config = state["config"]
    entry = {"source": source, "at": datetime.now().isoformat(), "ok": True, "indexed": 0,
             "deleted": 0, "error": None}
    with state["sync_lock"]:  # 단일 sqlite3.Connection 공유 쓰기 직렬화 (스펙 §5 P0)
        conn = state["conn"]  # 락 안에서 획득 — M6b 재구축이 커넥션을 교체해도 낡은 참조를 들지 않는다
        if conn is None:
            # 스키마 불일치 등으로 DB를 열지 못한 상태 — 예외를 던지면 scheduler_loop가 죽는다
            entry["ok"] = False
            entry["error"] = state.get("schema_mismatch") or "index.db를 열 수 없습니다 — 재구축이 필요합니다"
            _logger.error("%s 동기화 건너뜀(DB 없음): %s", source, entry["error"])
            state["log"].insert(0, entry)
            del state["log"][200:]
            return entry
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
            prev = indexer.get_sync_state(conn, source)
            rules_md = load_rules_md(cfg.rules_md_path)
            if source == "notes":
                result = sync_notes(cfg.notes_folders, cfg.exclude, prev)
            elif source == "local_docs":
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
                    renderer=_get_slide_renderer(state),
                )
            elif source == "outlook_mail":
                client = _get_outlook_client(state)
                result = sync_outlook_mail(
                    client, cfg.mail_folders, cfg.mail_since_days, cfg.exclude,
                    prev, batch_size=cfg.mail_batch_size,
                )
            elif source == "outlook_cal":
                client = _get_outlook_client(state)
                result = sync_outlook_cal(client, cfg.cal_past_days, cfg.cal_future_days, prev)
            elif source == "confluence":
                client = _get_atlassian_client(state, "confluence")
                result = sync_confluence(client, state["registry"].confluence_page_ids(),
                                         prev, cfg.data_dir / "confluence")
            else:  # jira
                client = _get_atlassian_client(state, "jira")
                result = sync_jira(client, state["registry"].jira_keys(),
                                   prev, cfg.data_dir / "jira")
            entry["indexed"] = indexer.index_documents(conn, result.documents, state["embedder"])
            entry["deleted"] = indexer.delete_documents(conn, source, result.deleted_ids)
            for doc in result.documents:
                if "summary_path" in doc.extra:
                    indexer.set_para_map(conn, doc.source_id, doc.extra["para_path"], doc.extra["summary_path"])
            indexer.set_sync_state(conn, source, result.state)
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
        except Exception as exc:
            conn.rollback()  # 실패한 트랜잭션의 부분 반영 방지 — 다음 동기화가 깨끗한 상태에서 시작
            entry["ok"] = False
            entry["error"] = f"{exc}\n{traceback.format_exc(limit=3)}"
        state["log"].insert(0, entry)
        del state["log"][200:]
    return entry


def create_app(config: Config, embedder=None, summarizer=None, answerer=None,
               outlook_client=None, atlassian_client=None, slide_renderer=None,
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

    # 사용량 카운팅 래퍼 (스펙 §10 P2) — 주입된 Fake 포함 모든 경로를 기록.
    # 래퍼는 기록만 하고 차단하지 않는다 — 차단은 run_sync 진입 게이트 한 곳에서만.
    tracker = UsageTracker(config.data_dir / "usage.json", config.daily_api_call_limit)
    embedder = CountingEmbedder(embedder, tracker)
    summarizer = CountingSummarizer(summarizer, tracker)

    conn = db.open_db(config.db_path)
    # 쓰기는 conn(run_sync 전용), 읽기는 read_conn — 동기화 쓰기 트랜잭션 중에도
    # /api/chat, /api/sources 같은 읽기 요청이 같은 커넥션을 공유하지 않게 분리한다.
    read_conn = db.open_db(config.db_path)
    state = {"config": config, "conn": conn, "read_conn": read_conn, "embedder": embedder,
             "summarizer": summarizer, "answerer": answerer, "log": [],
             "sync_lock": threading.Lock(), "outlook_client": outlook_client,
             "confluence_client": atlassian_client,
             "jira_client": atlassian_client,
             "registry": Registry(config.data_dir / "atlassian.json"),
             "usage": tracker,
             "resummarizing": False, "resummarize_lock": threading.Lock()}
    if slide_renderer is not None:
        state["slide_renderer"] = slide_renderer

    def _require_db() -> None:
        """DB를 만지는 엔드포인트 진입 가드 — 스키마 불일치 상태에서는 503으로 안내 (M6b 배너와 짝)."""
        if state["read_conn"] is None or state["conn"] is None:
            raise HTTPException(503, state.get("schema_mismatch") or "index.db를 열 수 없습니다 — 재구축이 필요합니다")

    app = FastAPI(title="llmsearch")
    app.state.llmsearch = state
    # Host 헤더 검증 — 로컬 전용 앱이 임의 Host로 오는 DNS 리바인딩류 요청을 받지 않게 함
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    async def scheduler_loop():
        while True:
            await asyncio.sleep(config.sync_interval_minutes * 60)
            for source in _scheduled_sources(state):
                try:
                    await asyncio.to_thread(run_sync, state, source)
                except Exception:  # run_sync는 내부에서 격리하지만, 어떤 예외에도 루프는 살아야 한다
                    _logger.exception("스케줄러 동기화 예외 격리: %s", source)

    @app.on_event("startup")
    async def _startup():
        if enable_scheduler:
            state["scheduler"] = asyncio.create_task(scheduler_loop())

    @app.on_event("shutdown")
    async def _shutdown():
        worker = state.get("outlook_worker")
        if worker is not None:
            try:
                worker.shutdown()
            except Exception:
                pass

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

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

    @app.post("/api/sync/{source}")
    def manual_sync(source: str):
        _require_db()
        if source not in SOURCES:
            raise HTTPException(404, f"unknown source: {source}")
        return run_sync(state, source)

    @app.get("/api/log")
    def log():
        return state["log"]

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

    @app.get("/api/para/projects")
    def para_projects():
        """summaries/Projects/ 하위 폴더 목록 — GUI 아카이브 섹션용 (스펙 §7.1 P1)."""
        _require_db()
        projects_dir = config.summaries_dir / "Projects"
        out = []
        if projects_dir.is_dir():
            for p in sorted(d for d in projects_dir.iterdir() if d.is_dir()):
                row = state["read_conn"].execute(
                    "SELECT COUNT(*) FROM documents WHERE para_path=?", (f"Projects/{p.name}",)
                ).fetchone()
                out.append({"name": p.name, "doc_count": row[0]})
        return out

    @app.post("/api/archive")
    def archive(payload: dict):
        name = str(payload.get("project", ""))
        _require_db()
        with state["sync_lock"]:  # 동기화 중 폴더 이동 금지 — 쓰기 직렬화
            try:
                return archive_project(state["conn"], config.summaries_dir, name)
            except KeyError as exc:
                raise HTTPException(404, exc.args[0])  # str(KeyError)는 따옴표가 붙어 UI에 그대로 노출됨
            except ValueError as exc:
                raise HTTPException(400, str(exc))

    @app.post("/api/open")
    def open_item(payload: dict):
        target = str(payload.get("url_or_path", ""))
        _require_db()
        try:
            if target.startswith("outlook:"):
                _get_outlook_client(state).open_item(target.removeprefix("outlook:"))
                return {"ok": True}
            if target.startswith("http://") or target.startswith("https://"):
                # M3부터 confluence/jira 문서의 url_or_path는 http(s) URL — 인덱스에 정확히
                # 등록된 값인지 검증 후에만 연다(CSRF로 임의 URL을 열게 하는 것 방지).
                row = state["read_conn"].execute(
                    "SELECT 1 FROM documents WHERE url_or_path=? LIMIT 1", (target,)
                ).fetchone()
                if row is None:
                    return {"ok": False, "error": "인덱스에 등록된 URL만 열 수 있습니다"}
                import os
                if hasattr(os, "startfile"):  # Windows 전용 — 기본 브라우저로 연다
                    import webbrowser
                    webbrowser.open(target)
                    return {"ok": True}
                return {"ok": False, "error": "파일 열기는 Windows에서만 지원됩니다"}
            # 로컬 경로 실행 전 검증: localhost API는 CSRF로 임의 사이트가 두드릴 수 있으므로
            # (M1 XSS와 같은 계열의 위협) 인덱스에 등록된 경로만 연다 — 임의 파일 실행 방지.
            resolved = str(Path(target).resolve())
            row = state["read_conn"].execute(
                "SELECT 1 FROM documents WHERE url_or_path=? LIMIT 1", (resolved,)
            ).fetchone()
            if row is None:  # local_docs/notes는 이미 resolve()된 문자열을 저장하지만 대비 차원의 폴백
                row = state["read_conn"].execute(
                    "SELECT 1 FROM documents WHERE url_or_path=? LIMIT 1", (target,)
                ).fetchone()
            if row is None:
                return {"ok": False, "error": "인덱스에 등록된 경로만 열 수 있습니다"}
            import os
            if hasattr(os, "startfile"):  # Windows 전용
                os.startfile(resolved)  # noqa: S606 — 위에서 인덱스 등록 여부를 검증한 경로만 실행
                return {"ok": True}
            return {"ok": False, "error": "파일 열기는 Windows에서만 지원됩니다"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/chat")
    def chat(payload: dict):
        _require_db()
        state["usage"].record("answer")
        question = payload.get("question", "")
        history = payload.get("history", [])

        def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
            return search.search(state["read_conn"], embedder, query, source_filter=source_filter,
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
