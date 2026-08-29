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
