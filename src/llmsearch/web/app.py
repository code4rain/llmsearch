from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import traceback
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import db, indexer, rebuild, search
from ..archive import archive_project
from ..atlassian.auth import diagnose, resolve_auth_candidates
from ..atlassian.registry import Registry
from ..chats import DEFAULT_TITLE, ChatStore, normalize_title
from ..config import Config
from ..connectors.confluence import sync_confluence
from ..connectors.jira import sync_jira
from ..connectors.local_docs import RETRY_SENTINEL, sync_local_docs
from ..connectors.notes import sync_notes
from ..connectors.outlook_cal import sync_outlook_cal
from ..connectors.outlook_mail import backlog_hint, sync_outlook_mail
from ..eval.golden import evaluate as golden_evaluate, parse_golden
from ..rules import load_rules_md, parse_rules_md
from ..usage import CountingEmbedder, CountingSummarizer, UsageTracker

STATIC_DIR = Path(__file__).parent / "static"
SOURCES = ("notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira")
RULES_TEMPLATE = "# 규칙 (rules.md)\n\n## 용어집\n\n## 분류 규칙\n\n## 요약 규칙\n\n## 답변 규칙\n"
_RULES_MAX_BYTES = 256 * 1024  # rules.md 파일 크기 상한 — 사고성 대용량 저장 방지 (스펙 M6 §3)
GOLDEN_TEMPLATE = (
    "# 골든 질문 세트 — 검색 상위 3위 적중률 측정 (목표 70%)\n"
    "# expect_source_id: 전체 경로 또는 경로 접미사(파일명). 동명 파일이 여러 폴더에 있으면 아무 쪽이나 적중.\n"
    "# - question: 프로젝트A 킥오프 언제?\n#   expect_source_id: kickoff.md\n"
)
_AUTH_EXPIRED_MSG = (
    "Atlassian 인증이 만료되었습니다. .env의 자격증명(ATLASSIAN_* 또는 서비스별 "
    "CONFLUENCE_*/JIRA_*)을 갱신한 뒤 다시 동기화하세요."
)
_logger = logging.getLogger(__name__)


def _is_local_origin(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:  # 예: "http://[" — 잘못된 IPv6 literal (fail-closed: 로컬이 아닌 것으로 취급)
        return False
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
                raise HTTPException(400, f"{key}는 YYYY-MM-DD 형식이어야 합니다")
            try:
                ok = date.fromisoformat(v).isoformat() == v  # 왕복 비교 — ISO 주차 표기(2026-W01-1) 등은 걸러낸다
            except ValueError:
                ok = False
            if not ok:
                raise HTTPException(400, f"{key}는 YYYY-MM-DD 형식이어야 합니다")
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
                result = sync_notes(cfg.notes_folders, cfg.exclude, prev, extra_files=[cfg.rules_md_path])
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
                    summary_rules=rules_md.get("요약 규칙", ""),
                    state=prev, prior_map=prior_map,
                    renderer=_get_slide_renderer(state),
                    force_reindex=bool(state.get("force_reindex_local_docs")),
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
            if source == "local_docs" and state.get("force_reindex_local_docs"):
                # 플래그는 커넥터가 정상 반환한 뒤에만 소비 — 마커도 이 시점에만 삭제 (스펙 M6 §6).
                # 영속(마커 삭제 + commit)이 먼저 — 그 뒤에야 인메모리 플래그를 내린다. 순서가
                # 반대면 커밋 전에 프로세스가 죽었을 때 마커는 남는데 플래그만 꺼진 상태가 된다.
                rebuild.clear_marker(conn)
                conn.commit()
                state["force_reindex_local_docs"] = False
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

    try:
        chat_store = ChatStore(config.data_dir / "chats.db")
        chat_store_error = None
    except Exception as exc:
        # 대화 저장소 장애가 채팅 기능을 볼모로 잡지 않게 — 세션 API만 503, 채팅은 무저장 폴백
        chat_store, chat_store_error = None, type(exc).__name__
        _logger.exception("chats.db를 열 수 없음 — 대화 저장 없이 기동")

    state = {"config": config, "conn": conn, "read_conn": read_conn, "embedder": embedder,
             "summarizer": summarizer, "answerer": answerer, "log": [],
             "sync_lock": threading.Lock(), "outlook_client": outlook_client,
             "confluence_client": atlassian_client,
             "jira_client": atlassian_client,
             "registry": Registry(config.data_dir / "atlassian.json"),
             "usage": tracker,
             "resummarizing": False, "resummarize_lock": threading.Lock(),
             "schema_mismatch": schema_mismatch, "rebuilding": False, "force_reindex_local_docs": False,
             "evaluating": False, "evaluate_lock": threading.Lock(),
             "chat_store": chat_store, "chat_store_error": chat_store_error}
    if slide_renderer is not None:
        state["slide_renderer"] = slide_renderer
    if conn is not None and rebuild.marker_present(conn):
        state["force_reindex_local_docs"] = True  # 이전 재구축이 완료되지 않음 — 배너 [재개]
        _logger.warning("이전 재구축이 완료되지 않았습니다 — 설정 탭에서 [재개]하세요")

    def _require_db() -> None:
        """DB를 만지는 엔드포인트 진입 가드 — 스키마 불일치 상태에서는 503으로 안내 (M6b 배너와 짝)."""
        if state["read_conn"] is None or state["conn"] is None:
            raise HTTPException(503, state.get("schema_mismatch") or "index.db를 열 수 없습니다 — 재구축이 필요합니다")

    def _require_chat_store() -> ChatStore:
        store = state.get("chat_store")
        if store is None:
            raise HTTPException(503, f"대화 저장소를 열 수 없습니다: {state.get('chat_store_error')}")
        return store

    app = FastAPI(title="llmsearch")
    app.state.llmsearch = state
    # Host 헤더 검증 — 로컬 전용 앱이 임의 Host로 오는 DNS 리바인딩류 요청을 받지 않게 함
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    async def scheduler_loop():
        while True:
            await asyncio.sleep(config.sync_interval_minutes * 60)
            if state.get("rebuilding"):
                continue
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
        store = state.get("chat_store")
        if store is not None:
            try:
                store.close()
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

    @app.post("/api/sync/{source}", dependencies=[Depends(local_origin_only)])
    def manual_sync(source: str):
        _require_db()
        if source not in SOURCES:
            raise HTTPException(404, f"unknown source: {source}")
        return run_sync(state, source)

    @app.get("/api/log")
    def log():
        return state["log"]

    @app.get("/api/usage")
    def usage_status():
        t = state["usage"]
        return {"today": t.today_by_kind(), "total": t.today_total(), "limit": t.daily_limit,
                "indexing_allowed": t.indexing_allowed(),
                "days": [{"date": d, "total": n} for d, n in t.recent_days(7)]}

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
        state["evaluating"] = True  # rebuild.claim이 sync_lock 아래서 이 값을 보게 먼저 세운다 (중간에 인덱스가 지워지는 것 방지)
        conn = None
        try:
            if state.get("rebuilding"):
                raise HTTPException(409, "인덱스 재구축이 진행 중입니다 — 완료 후 평가하세요")
            conn = db.open_db(config.db_path)  # try 안 — open 실패로 락이 영구 점유되지 않게
            report = golden_evaluate(conn, embedder, cases)
        except HTTPException:
            raise
        except Exception as exc:
            # 예외 메시지에 자격증명이 섞일 수 있어 클래스명만 노출 — 로그도 클래스명만 (CLAUDE.md 보안)
            _logger.error("골든 평가 실패: %s", type(exc).__name__)
            raise HTTPException(502, f"평가 실패: {type(exc).__name__}")
        finally:
            if conn is not None:
                conn.close()
            state["evaluating"] = False
            state["evaluate_lock"].release()
        target = 0.7  # 상위 스펙 §1 성공 기준
        return {**{k: report[k] for k in ("total", "hit_at_3", "rate", "cases")},
                "target": target, "pass": report["rate"] >= target}

    @app.get("/api/status")
    def status():
        read_conn = state["read_conn"]
        return {"schema_mismatch": state.get("schema_mismatch"),
                "rebuild_in_progress": read_conn is not None and rebuild.marker_present(read_conn),
                "rebuilding": bool(state.get("rebuilding")),
                "resummarizing": bool(state.get("resummarizing")),
                "evaluating": bool(state.get("evaluating"))}

    @app.post("/api/rebuild", dependencies=[Depends(local_origin_only)])
    def rebuild_index(payload: dict):
        """인덱스 재구축 (스펙 M6 §6). 초기화·복원은 동기, 재수집은 백그라운드 — 진행은 소스 탭·로그 탭."""
        force = payload.get("force") is True
        try:
            rebuild.precheck(state, force=force)
            rebuild.claim(state)  # 여기부터 rebuilding=True — 동시 POST는 409
        except rebuild.RebuildRefused as exc:
            return JSONResponse(status_code=409, content={"detail": exc.detail, "missing_folders": exc.missing_folders})
        try:
            info = rebuild.recover_schema_mismatch(state) if state.get("schema_mismatch") else rebuild.reset_index(state)
            targets = _scheduled_sources(state)
            rebuild.start_resync(state, run_sync, targets)
        except Exception:
            rebuild.release(state)
            raise
        return {"ok": True, "phase": "resync", "targets": targets, **info}

    @app.post("/api/rebuild/resume", dependencies=[Depends(local_origin_only)])
    def rebuild_resume():
        _require_db()
        if not rebuild.marker_present(state["read_conn"]):
            return JSONResponse(status_code=409, content={"detail": "재개할 재구축이 없습니다", "missing_folders": []})
        try:
            rebuild.precheck(state, force=True)  # 상한 도달 시 게이트에 막혀 도는 대신 사유를 바로 알린다
            rebuild.claim(state)
        except rebuild.RebuildRefused as exc:
            return JSONResponse(status_code=409, content={"detail": exc.detail, "missing_folders": []})
        state["force_reindex_local_docs"] = True  # 마커가 진실 원천 — claim 성공 뒤 명시적으로 맞춰둔다
        try:
            targets = _scheduled_sources(state)
            rebuild.start_resync(state, run_sync, targets)
        except Exception:
            rebuild.release(state)
            raise
        return {"ok": True, "phase": "resync", "targets": targets}

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
        if not state["usage"].indexing_allowed():
            raise HTTPException(409, "일일 API 호출 상한 도달 — 상한이 초기화된 뒤 재요약하세요")
        if not state["resummarize_lock"].acquire(blocking=False):  # check-then-set 경쟁 방지 (스레드풀)
            raise HTTPException(409, "재요약이 이미 진행 중입니다")
        try:
            with state["sync_lock"]:
                # rebuilding 판정과 resummarizing 표시를 같은 락 임계구역 첫 줄에 둔다 — claim()도
                # 같은 락 안에서 두 플래그를 검사하므로(M6b 리뷰 Important 1·2), 이 순서가 아니면
                # 재구축과 전체 재요약이 서로를 못 보고 동시에 진행돼 LLM을 이중 호출할 수 있다.
                if state.get("rebuilding"):
                    raise HTTPException(409, "인덱스 재구축이 진행 중입니다 — 완료 후 재요약하세요")
                state["resummarizing"] = True  # M6b rebuild 사전 검사(스펙 §6)가 읽는 표시
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
            state["resummarize_lock"].release()

    @app.post("/api/atlassian/register", dependencies=[Depends(local_origin_only)])
    def atlassian_register(payload: dict):
        try:
            return state["registry"].add(str(payload.get("url", "")))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/atlassian/registrations")
    def atlassian_registrations():
        return state["registry"].list()

    @app.delete("/api/atlassian/registrations", dependencies=[Depends(local_origin_only)])
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

    @app.post("/api/archive", dependencies=[Depends(local_origin_only)])
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

    @app.post("/api/open", dependencies=[Depends(local_origin_only)])
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
            if hasattr(os, "startfile"):  # Windows 전용
                os.startfile(resolved)  # noqa: S606 — 위에서 인덱스 등록 여부를 검증한 경로만 실행
                return {"ok": True}
            return {"ok": False, "error": "파일 열기는 Windows에서만 지원됩니다"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
        question = payload.get("question", "")
        if store is not None and not str(question).strip():
            raise HTTPException(400, "질문이 비어 있습니다")  # 빈 user 블록은 세션의 후속 질문을 전부 깨뜨린다
        state["usage"].record("answer")
        if store is not None:
            store.append(session_id, "user", question, filters=filters)  # 스트림 전에 저장 — 중단돼도 질문은 남는다
            try:
                if store.get_title(session_id) == DEFAULT_TITLE:
                    store.set_title(session_id, question)
            except KeyError:
                pass  # 그 사이 삭제된 세션 — 저장 실패와 같은 관용

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

    return app
