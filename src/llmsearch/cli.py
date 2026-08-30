"""llmsearch CLI — Claude 스킬이 호출하는 결정적 도구 (스펙 docs/superpowers/specs/2026-08-31-claude-skill-design.md).

모든 명령은 GUI와 같은 함수(search.search / run_sync / UsageTracker)를 호출한다 — 로직을 복제하지 않는다.
exit: 0 성공 / 1 실행 실패 / 2 설정·인자 오류 / 3 서버 실행 중 / 4 스키마 불일치
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Callable

from . import db, indexer, rebuild
from .config import Config, ConfigNotFound, load_config, load_env, resolve_config_path
from .usage import UsageTracker
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
