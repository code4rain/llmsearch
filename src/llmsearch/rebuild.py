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
from datetime import datetime
from pathlib import Path

from . import db, indexer
from .connectors.local_docs import RETRY_SENTINEL

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
    """제자리 초기화 — documents 전 행·local_docs 외 sync_state 삭제 + 마커, 단일 트랜잭션.

    실패 시 롤백한다 — 부분 삭제가 커밋되면 이후 무관한 sync가 그 상태를 그대로 이어받는다.
    """
    with state["sync_lock"]:
        conn: sqlite3.Connection = state["conn"]
        try:
            deleted = indexer.delete_all_documents(conn)
            conn.execute("DELETE FROM sync_state WHERE source_type != 'local_docs'")
            set_marker(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        state["force_reindex_local_docs"] = True
    logger.info("인덱스 초기화 — documents %d건 삭제, para_map·local_docs 상태 보존", deleted)
    return {"documents_deleted": deleted}


def claim(state: dict) -> None:
    """precheck 통과 직후 rebuilding을 원자적으로 선점 — 동시 POST 두 건이 둘 다 초기화하는 것을 막는다.

    재요약(resummarizing)도 함께 거부한다 — 재구축이 local_docs sync_state를 센티널로 덮어쓰는
    동안 전체 재요약이 끼어들면 이중으로 LLM을 호출하게 된다. 선점 후 초기화·start_resync가
    실패하면 호출자가 release()로 되돌린다.
    """
    with state["sync_lock"]:
        if state.get("rebuilding") or state.get("resummarizing"):
            raise RebuildRefused("재구축 또는 재요약이 진행 중입니다 — 끝난 뒤 다시 시도하세요")
        state["rebuilding"] = True


def release(state: dict) -> None:
    state["rebuilding"] = False


def start_resync(state: dict, run_sync: Callable[[dict, str], dict], sources: Sequence[str]) -> threading.Thread:
    """백그라운드 재수집 — 수천 문서·메일 1년치는 수십 분이 걸리므로 HTTP 요청 안에서 기다리지 않는다.

    호출자가 claim()으로 rebuilding을 이미 선점한 상태여야 한다(선점 안 됐으면 여기서 선점).
    마커는 여기서 지우지 않는다 — local_docs run_sync가 force_reindex 플래그를 소비할 때 지운다.
    """
    if not state.get("rebuilding"):
        claim(state)
    targets = list(sources)

    def target():
        try:
            for source in targets:
                entry = run_sync(state, source)
                logger.info("재수집 %s: ok=%s indexed=%s", source, entry["ok"], entry["indexed"])
        finally:
            release(state)

    thread = threading.Thread(target=target, name="llmsearch-rebuild", daemon=True)
    try:
        thread.start()
    except BaseException:
        release(state)  # start 실패로 영구 "진행 중"이 되지 않게
        raise
    state["rebuild_thread"] = thread  # start 성공 뒤에만 노출 — 실패한 스레드를 wait_resync가 join하지 않게
    return thread


def recover_schema_mismatch(state: dict) -> dict:
    """스키마 불일치 상태의 재구축 — legacy 매핑 회수 → 파일 백업·재생성 → 매핑 복원 → 커넥션 교체.

    손상된 index.db는 지우지 않고 타임스탬프를 붙여 옆에 남긴다 — 새 DB가 실제로 열리는 것을
    증명하기 전에 지우면, 재오픈이 실패했을 때 legacy 매핑을 되돌릴 방법이 없어진다.
    """
    cfg = state["config"]
    with state["sync_lock"]:
        rows, local_state = db.read_legacy_maps(cfg.db_path)
        db_path = Path(cfg.db_path)
        backup_path = db_path.with_name(db_path.name + f".corrupt-{datetime.now():%Y%m%d-%H%M%S}")
        renamed = False
        if db_path.exists():
            db_path.rename(backup_path)  # unlink 대신 rename — 새 DB 오픈 실패 시 legacy 매핑 복구 경로 보존
            renamed = True
            logger.info("손상된 index.db 백업: %s", backup_path)
        conn = read_conn = None
        try:
            for suffix in ("-wal", "-shm"):
                Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)  # 열린 커넥션 없음(conn is None)
            conn = db.open_db(cfg.db_path)
            read_conn = db.open_db(cfg.db_path)
            for sid, para_path, summary_path in rows:
                conn.execute("INSERT OR REPLACE INTO para_map(source_id, para_path, summary_path) VALUES (?,?,?)",
                             (sid, para_path, summary_path))
            files = dict(local_state.get("files", {})) if isinstance(local_state, dict) else {}
            for sid, _para, _summary in rows:
                # 상태가 유실됐어도 para_map에 있는 sid는 상태에 남긴다 — run_sync의 prior_map은 files 키로
                # 만들어지므로, 비어 있으면 prior=None → _place가 해시 접미사 중복 md를 만든다 (스펙 §10 C1).
                # 센티널은 실제 (mtime, size)와 결코 일치하지 않아 재요약을 강제한다(md 재사용 아님) —
                # 정확한 시그니처를 잃었으니 안전 쪽으로 재요약을 택한다.
                files.setdefault(sid, list(RETRY_SENTINEL))
            if files or local_state:
                indexer.set_sync_state(conn, "local_docs", {**(local_state or {}), "files": files})
            set_marker(conn)
            conn.commit()
        except BaseException:
            # 새 index.db가 실제로 열리는 것을 증명하기 전에 실패 — 백업을 원위치로 되돌려 legacy 매핑을
            # 다음 시도에서도 회수 가능하게 한다. state는 건드리지 않는다(conn은 여전히 None으로 남는다).
            for c in (conn, read_conn):
                if c is not None:
                    c.close()
            for suffix in ("", "-wal", "-shm"):
                Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)
            if renamed:
                backup_path.rename(db_path)
            raise
        state["conn"], state["read_conn"] = conn, read_conn
        state["schema_mismatch"] = None
        state["force_reindex_local_docs"] = True
    if not rows:
        logger.warning("legacy 매핑을 회수하지 못함 — local_docs 전량 재요약 (요약 API 소모)")
    return {"legacy_maps_recovered": len(rows), "documents_deleted": 0, "backup": str(backup_path)}


def run_cli(state: dict, run_sync: Callable[[dict, str], dict], sources: Sequence[str],
            yes: bool = False, force: bool = False, input_fn: Callable[[str], str] = input,
            out: Callable[[str], None] = print) -> int:
    """헤드리스 재구축 — 서버 기동 전에 동기로 초기화·재수집.

    종료코드: 0=전 소스 성공, 1=초기화는 성공했으나 일부 소스 재수집 실패(서버는 계속 기동), 2=거부/취소.
    """
    try:
        precheck(state, force=force)  # 확인 프롬프트 전에 거부 조건을 먼저 — 확인한 뒤 거부되면 혼란
    except RebuildRefused as exc:
        out(f"재구축 거부: {exc.detail}" + (" (--force로 건너뛰기 가능)" if exc.missing_folders else ""))
        return 2
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
        claim(state)
    except RebuildRefused as exc:
        out(f"재구축 거부: {exc.detail}")
        return 2
    try:
        info = recover_schema_mismatch(state) if state.get("schema_mismatch") else reset_index(state)
    except Exception:
        release(state)
        raise
    out(f"초기화 완료: {info}")
    if info.get("legacy_maps_recovered") == 0:
        out("⚠️ 요약 md 매핑을 회수하지 못했습니다 — local_docs가 전량 재요약됩니다(요약 API 소모)")
    failed = []
    try:
        for source in sources:
            entry = run_sync(state, source)
            out(f"재수집 {source}: ok={entry['ok']} indexed={entry['indexed']}"
                + (f" error={entry['error'].splitlines()[0]}" if entry["error"] else ""))
            if not entry["ok"]:
                failed.append(source)
    finally:
        release(state)
    if failed:
        out(f"재수집 실패 소스: {', '.join(failed)} — 로그 탭/스케줄러 라운드에서 재시도됩니다")
        return 1
    return 0
