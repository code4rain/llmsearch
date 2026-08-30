"""llmsearch CLI — Claude 스킬이 호출하는 결정적 도구 (스펙 docs/superpowers/specs/2026-08-31-claude-skill-design.md).

모든 명령은 GUI와 같은 함수(search.search / run_sync / UsageTracker)를 호출한다 — 로직을 복제하지 않는다.
exit: 0 성공 / 1 실행 실패 / 2 설정·인자 오류 / 3 서버 실행 중 / 4 스키마 불일치
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Callable

from . import db, indexer, rebuild
from . import search as search_mod
from .config import Config, ConfigNotFound, load_config, load_env, resolve_config_path
from .usage import CountingEmbedder, UsageTracker
from .web.app import SOURCES

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_SERVER_RUNNING, EXIT_SCHEMA = 0, 1, 2, 3, 4
DEFAULT_PORT = 8642


class CliError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _load(args) -> tuple[Path, Config]:
    try:
        path = resolve_config_path(args.config)
    except ConfigNotFound as exc:
        raise CliError(EXIT_USAGE, str(exc)) from exc
    return path, load_config(path)


def _open_index(cfg: Config, allow_create: bool = False) -> sqlite3.Connection:
    """읽기용 커넥션. open_db는 없는 파일을 만들어 버리므로 존재를 먼저 확인한다 (sync만 생성 허용)."""
    if not allow_create and not cfg.db_path.exists():
        raise CliError(EXIT_USAGE, f"인덱스가 없습니다: {cfg.db_path} — GUI 또는 `llmsearch sync all`로 생성하세요")
    try:
        return db.open_db(cfg.db_path)
    except db.SchemaMismatchError as exc:
        raise CliError(EXIT_SCHEMA, str(exc)) from exc


def _emit(args, payload: dict, markdown: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(markdown)


# ---- status ---------------------------------------------------------------

def cmd_status(args) -> int:
    path, cfg = _load(args)
    conn = _open_index(cfg)
    try:
        tracker = UsageTracker(cfg.data_dir / "usage.json", cfg.daily_api_call_limit)
        sources = []
        for s in SOURCES:
            n = conn.execute("SELECT COUNT(*) FROM documents WHERE source_type=?", (s,)).fetchone()[0]
            sources.append({"source": s, "doc_count": n, "synced": bool(indexer.get_sync_state(conn, s))})
        payload = {
            "config": str(path), "db": str(cfg.db_path), "schema_version": db.SCHEMA_VERSION,
            "vector_backend": "sqlite-vec" if db.HAS_SQLITE_VEC else "numpy",
            "rebuild_in_progress": rebuild.marker_present(conn), "sources": sources,
            "usage_today": tracker.today_total(), "usage_by_kind": tracker.today_by_kind(),
            "daily_limit": cfg.daily_api_call_limit,
        }
    finally:
        conn.close()
    lines = [f"# llmsearch status", f"- config: `{payload['config']}`", f"- db: `{payload['db']}` "
             f"(schema v{payload['schema_version']}, {payload['vector_backend']})",
             f"- usage today: {payload['usage_today']} (limit {payload['daily_limit'] or '없음'})",
             f"- rebuild in progress: {payload['rebuild_in_progress']}", "",
             "| source | docs | synced |", "|---|---|---|"]
    lines += [f"| {s['source']} | {s['doc_count']} | {'yes' if s['synced'] else 'no'} |" for s in sources]
    _emit(args, payload, "\n".join(lines))
    return EXIT_OK


# ---- search ---------------------------------------------------------------

def _parse_filters(args) -> dict:
    """web.app._validate_filters와 같은 규칙 (소스명·YYYY-MM-DD 왕복·발신자는 메일 소스에서만)."""
    out: dict = {"source_filter": None, "date_from": None, "date_to": None, "sender": None}
    if args.source:
        unknown = [s for s in args.source if s not in SOURCES]
        if unknown:
            raise CliError(EXIT_USAGE, f"알 수 없는 소스: {', '.join(unknown)} (가능: {', '.join(SOURCES)})")
        out["source_filter"] = [s for s in SOURCES if s in args.source]
    for key, value in (("date_from", args.date_from), ("date_to", args.date_to)):
        if value:
            try:
                ok = date.fromisoformat(value).isoformat() == value
            except ValueError:
                ok = False
            if not ok:
                raise CliError(EXIT_USAGE, f"--{key.replace('date_', '')}는 YYYY-MM-DD 형식이어야 합니다: {value}")
            out[key] = value
    if args.sender:
        sender = args.sender.strip()
        if len(sender) > 200:
            raise CliError(EXIT_USAGE, "--sender는 200자 이하여야 합니다")
        if out["source_filter"] and "outlook_mail" not in out["source_filter"]:
            raise CliError(EXIT_USAGE, "발신자 필터는 메일 소스에서만 동작합니다 — --source에 outlook_mail을 포함하거나 비우세요")
        out["sender"] = sender
    return out


def _resolve_embedder(args, cfg: Config):
    """(embedder|None, fts_only). 키가 없으면 FTS 전용으로 강등하고 stderr에 알린다."""
    if args.fts_only:
        return None, True
    embedder = args._embedder
    if embedder is None:
        if not os.environ.get("GEMINI_API_KEY"):
            print("경고: GEMINI_API_KEY 없음 — FTS 전용 검색 (GUI의 하이브리드 순위와 다름). "
                  "~/.llmsearch/.env에 키를 넣으면 동일한 순위가 된다", file=sys.stderr)
            return None, True
        from .embeddings import GeminiEmbeddings  # 지연 import — 키 없는 환경에서 SDK를 건드리지 않는다
        embedder = GeminiEmbeddings(model=cfg.embed_model)
    tracker = UsageTracker(cfg.data_dir / "usage.json", cfg.daily_api_call_limit)
    return CountingEmbedder(embedder, tracker), False  # GUI와 동일하게 사용량 기록


def _hit_markdown(i: int, h: dict, excerpt: bool) -> str:
    lines = [f"{i}. **{h['title']}** — {h['source_type']} · {h['updated_at'][:10]} · score {h['score']:.4f}",
             f"   path: {h['url_or_path']}", f"   id: {h['source_id']}"]
    if not h["content_indexed"]:
        lines.append("   (본문 미인덱싱 — 메타데이터만)")
    if h["snippet"]:
        lines.append(f"   {h['snippet']}")
    if excerpt:
        lines += ["   > " + ln for ln in h["excerpt"].splitlines() if ln.strip()]
    return "\n".join(lines)


def cmd_search(args) -> int:
    _, cfg = _load(args)
    filters = _parse_filters(args)
    embedder, fts_only = _resolve_embedder(args, cfg)
    conn = _open_index(cfg)
    try:
        hits = search_mod.search(conn, embedder, args.query, k=args.k, **filters)
    finally:
        conn.close()
    rows = [asdict(h) for h in hits]
    payload = {"query": args.query, "fts_only": fts_only, "filters": filters, "hits": rows}
    mode = "fts-only" if fts_only else "hybrid"
    md = [f'## "{args.query}" — {len(rows)}건 ({mode})']
    md += [_hit_markdown(i, h, args.excerpt) for i, h in enumerate(rows, 1)] or ["(히트 없음)"]
    _emit(args, payload, "\n".join(md))
    return EXIT_OK


# ---- parser / main --------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # --config/--json은 서브커맨드 뒤에 오는 호출(`status --json`)이 테스트 계약이므로,
    # 최상위 파서가 아니라 공유 parent로 각 서브파서에 붙인다 — argparse는 최상위 옵션을
    # 서브커맨드 앞에서만 받아준다.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="생략 시 $LLMSEARCH_CONFIG 또는 ~/.llmsearch/config.yaml")
    common.add_argument("--json", action="store_true", help="JSON 출력 (기본: 마크다운)")

    p = argparse.ArgumentParser(prog="llmsearch", description="llmsearch 인덱스 CLI (Claude 스킬용)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common], help="인덱스·소스·사용량 상태").set_defaults(func=cmd_status)

    s = sub.add_parser("search", parents=[common],
                        help="하이브리드 검색 — 히트(출처·발췌)만 반환, 답변은 호출자가 작성")
    s.add_argument("query")
    s.add_argument("--source", action="append", default=[], help=f"소스 필터 (반복 가능): {', '.join(SOURCES)}")
    s.add_argument("--from", dest="date_from", default=None, help="YYYY-MM-DD")
    s.add_argument("--to", dest="date_to", default=None, help="YYYY-MM-DD")
    s.add_argument("--sender", default=None, help="발신자 (outlook_mail)")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--fts-only", action="store_true", help="벡터 검색 생략 (키 없을 때 자동)")
    s.add_argument("--excerpt", action="store_true", help="마크다운에 발췌(≤6000자) 포함")
    s.set_defaults(func=cmd_search)

    return p


def main(argv: list[str] | None = None, *, embedder=None, app_factory: Callable | None = None,
         server_alive: Callable[[int], bool] | None = None) -> int:
    """테스트는 embedder(FakeEmbeddings)·app_factory·server_alive를 주입한다; 실구현은 지연 import."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 오류 → exit 2 통일
        return EXIT_USAGE if exc.code else EXIT_OK
    args._embedder, args._app_factory, args._server_alive = embedder, app_factory, server_alive
    load_env()
    try:
        return args.func(args)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except Exception as exc:  # 트레이스백 대신 한 줄 — 키·경로 평문 규칙은 각 예외 메시지가 지킨다
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
