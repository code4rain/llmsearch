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
    with state["sync_lock"]:  # 단일 sqlite3.Connection 공유 쓰기 직렬화 (스펙 §5 P0)
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
             "summarizer": summarizer, "answerer": answerer, "log": [],
             "sync_lock": threading.Lock()}

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
