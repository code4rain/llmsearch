from __future__ import annotations

import asyncio
import json
import threading
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import db, indexer, search
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

STATIC_DIR = Path(__file__).parent / "static"
SOURCES = ("notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira")


def _get_outlook_client(state):
    """실 클라이언트 지연 생성 — 테스트는 create_app 주입으로 이 경로를 타지 않는다."""
    if state.get("outlook_client") is None:
        from ..outlook.com_client import ThreadedOutlookClient
        from ..outlook.com_worker import ComWorker

        worker = ComWorker()
        state["outlook_worker"] = worker
        state["outlook_client"] = ThreadedOutlookClient(worker)
    return state["outlook_client"]


def _get_atlassian_client(state):
    """3단 폴백 자동 진단으로 클라이언트 지연 생성 (스펙 §7.2 P0). 진단 결과는 세션 캐시.

    자격증명(.env) 부재는 diagnose()가 안내 메시지와 함께 RuntimeError로 알린다.
    base URL 미설정은 자격증명이 있을 때만 별도로 더 구체적인 안내를 준다 — 그래야
    두 조건이 동시에 비어 있는 흔한 케이스(로컬 개발/테스트)에서도 자격증명 안내가
    먼저 보인다.
    """
    if state.get("atlassian_client") is None:
        cfg = state["config"]
        candidates = resolve_auth_candidates()
        if candidates and not cfg.confluence_base_url and not cfg.jira_base_url:
            raise RuntimeError(
                "config.yaml의 atlassian.confluence_base_url / jira_base_url을 설정하세요"
            )
        from ..atlassian.http_client import HttpAtlassianClient

        def factory(auth):
            return HttpAtlassianClient(cfg.confluence_base_url, cfg.jira_base_url, auth)

        client, _auth = diagnose(candidates, factory)
        state["atlassian_client"] = client
    return state["atlassian_client"]


def run_sync(state: dict, source: str) -> dict:
    """커넥터 1개 동기화 실행. 실패는 소스별로 격리해 로그에 남긴다 (스펙 §5)."""
    cfg: Config = state["config"]
    conn = state["conn"]
    entry = {"source": source, "at": datetime.now().isoformat(), "ok": True, "indexed": 0,
             "deleted": 0, "error": None}
    with state["sync_lock"]:  # 단일 sqlite3.Connection 공유 쓰기 직렬화 (스펙 §5 P0)
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
                client = _get_atlassian_client(state)
                result = sync_confluence(client, state["registry"].confluence_page_ids(),
                                         prev, cfg.data_dir / "confluence")
            else:  # jira
                client = _get_atlassian_client(state)
                result = sync_jira(client, state["registry"].jira_keys(),
                                   prev, cfg.data_dir / "jira")
            entry["indexed"] = indexer.index_documents(conn, result.documents, state["embedder"])
            entry["deleted"] = indexer.delete_documents(conn, source, result.deleted_ids)
            for doc in result.documents:
                if "summary_path" in doc.extra:
                    indexer.set_para_map(conn, doc.source_id, doc.extra["para_path"], doc.extra["summary_path"])
            indexer.set_sync_state(conn, source, result.state)
        except Exception as exc:
            conn.rollback()  # 실패한 트랜잭션의 부분 반영 방지 — 다음 동기화가 깨끗한 상태에서 시작
            entry["ok"] = False
            entry["error"] = f"{exc}\n{traceback.format_exc(limit=3)}"
        state["log"].insert(0, entry)
        del state["log"][200:]
    return entry


def create_app(config: Config, embedder=None, summarizer=None, answerer=None,
               outlook_client=None, atlassian_client=None, enable_scheduler: bool = True) -> FastAPI:
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
    # 쓰기는 conn(run_sync 전용), 읽기는 read_conn — 동기화 쓰기 트랜잭션 중에도
    # /api/chat, /api/sources 같은 읽기 요청이 같은 커넥션을 공유하지 않게 분리한다.
    read_conn = db.open_db(config.db_path)
    state = {"config": config, "conn": conn, "read_conn": read_conn, "embedder": embedder,
             "summarizer": summarizer, "answerer": answerer, "log": [],
             "sync_lock": threading.Lock(), "outlook_client": outlook_client,
             "atlassian_client": atlassian_client,
             "registry": Registry(config.data_dir / "atlassian.json")}

    app = FastAPI(title="llmsearch")
    app.state.llmsearch = state
    # Host 헤더 검증 — 로컬 전용 앱이 임의 Host로 오는 DNS 리바인딩류 요청을 받지 않게 함
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    async def scheduler_loop():
        while True:
            await asyncio.sleep(config.sync_interval_minutes * 60)
            for source in SOURCES:
                await asyncio.to_thread(run_sync, state, source)

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
        out = []
        for source in SOURCES:
            row = read_conn.execute("SELECT COUNT(*) FROM documents WHERE source_type=?", (source,)).fetchone()
            last = next((e for e in state["log"] if e["source"] == source), None)
            entry = {"source": source, "doc_count": row[0],
                     "last_sync": last["at"] if last else None,
                     "last_error": last["error"] if last else None}
            if source == "outlook_mail":
                entry["backlog"] = backlog_hint(indexer.get_sync_state(read_conn, source))
            out.append(entry)
        return out

    @app.post("/api/sync/{source}")
    def manual_sync(source: str):
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

    @app.post("/api/open")
    def open_item(payload: dict):
        target = str(payload.get("url_or_path", ""))
        try:
            if target.startswith("outlook:"):
                _get_outlook_client(state).open_item(target.removeprefix("outlook:"))
                return {"ok": True}
            if target.startswith("http://") or target.startswith("https://"):
                # M3부터 confluence/jira 문서의 url_or_path는 http(s) URL — 인덱스에 정확히
                # 등록된 값인지 검증 후에만 연다(CSRF로 임의 URL을 열게 하는 것 방지).
                row = read_conn.execute(
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
            row = read_conn.execute(
                "SELECT 1 FROM documents WHERE url_or_path=? LIMIT 1", (resolved,)
            ).fetchone()
            if row is None:  # local_docs/notes는 이미 resolve()된 문자열을 저장하지만 대비 차원의 폴백
                row = read_conn.execute(
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
        question = payload.get("question", "")
        history = payload.get("history", [])

        def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
            return search.search(read_conn, embedder, query, source_filter=source_filter,
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
