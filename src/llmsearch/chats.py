"""대화 세션 저장소 — data_dir/chats.db (스펙 M8 §2).

인덱스(index.db)와 분리한다: 인덱스는 소모품이라 rebuild가 지우지만 대화는 사용자 산출물이다.
단일 커넥션을 웹 스레드풀이 공유하므로 읽기·쓰기 모두 자체 락으로 직렬화한다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "1"
DEFAULT_TITLE = "새 대화"
TITLE_MAX = 60
ROLES = ("user", "assistant")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]', filters_json TEXT NOT NULL DEFAULT 'null',
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


def normalize_title(text: str) -> str:
    """공백 정규화 후 60자 절단, 빈 값이면 기본 제목."""
    return " ".join((text or "").split())[:TITLE_MAX] or DEFAULT_TITLE


def filters_label(filters: dict | None) -> str:
    """저장된 필터를 한 줄로 — UI filtersLabel()과 같은 형식 (export용)."""
    if not filters:
        return ""
    parts = []
    if filters.get("source_filter"):
        parts.append("소스=" + ",".join(filters["source_filter"]))
    if filters.get("date_from") or filters.get("date_to"):
        parts.append(f"기간={filters.get('date_from') or ''}~{filters.get('date_to') or ''}")
    if filters.get("sender"):
        parts.append("발신자=" + filters["sender"])
    return " · ".join(parts)


def _now() -> str:
    return datetime.now().isoformat()


class ChatStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")  # 커넥션 단위 설정 — cascade가 실제로 동작하게
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            self._conn.commit()
        elif row[0] != SCHEMA_VERSION:
            self._conn.close()
            raise RuntimeError(f"chats.db schema v{row[0]} != v{SCHEMA_VERSION}")

    # --- 내부 (락 보유 상태에서 호출) ---
    def _require(self, session_id: int) -> tuple:
        row = self._conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row

    def _messages(self, session_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, role, content, sources_json, filters_json, created_at FROM messages "
            "WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2], "sources": json.loads(r[3]),
                 "filters": json.loads(r[4]), "created_at": r[5]} for r in rows]

    # --- 공개 API ---
    def create_session(self, title: str = DEFAULT_TITLE) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute("INSERT INTO sessions(title, created_at, updated_at) VALUES (?,?,?)",
                                     (normalize_title(title), now, now))
            self._conn.commit()
            return cur.lastrowid

    def get_title(self, session_id: int) -> str:
        with self._lock:
            return self._require(session_id)[1]

    def set_title(self, session_id: int, title: str) -> None:
        with self._lock:
            self._require(session_id)
            self._conn.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                               (normalize_title(title), _now(), session_id))
            self._conn.commit()

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.title, s.created_at, s.updated_at, "
                "(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) "
                "FROM sessions s ORDER BY s.updated_at DESC, s.id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3], "message_count": r[4]}
                for r in rows]

    def get_session(self, session_id: int) -> dict:
        with self._lock:
            sid, title, created, updated = self._require(session_id)
            return {"id": sid, "title": title, "created_at": created, "updated_at": updated,
                    "messages": self._messages(session_id)}

    def append(self, session_id: int, role: str, content: str,
               sources: list[dict] | None = None, filters: dict | None = None) -> int:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        now = _now()
        with self._lock:
            self._require(session_id)
            cur = self._conn.execute(
                "INSERT INTO messages(session_id, role, content, sources_json, filters_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, role, content, json.dumps(sources or [], ensure_ascii=False),
                 json.dumps(filters, ensure_ascii=False), now))
            self._conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            self._conn.commit()
            return cur.lastrowid

    def history(self, session_id: int, limit: int = 20, max_chars: int = 40_000) -> list[dict]:
        """Claude 컨텍스트용 이력 — sources 제외. 첫 메시지는 반드시 user(Messages API 규칙),
        누적 길이가 max_chars를 넘으면 가장 오래된 메시지부터 role 무관하게 2건씩 제거."""
        with self._lock:
            self._require(session_id)
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit)).fetchall()
        msgs = [{"role": r, "content": c} for r, c in reversed(rows)]
        if msgs and msgs[0]["role"] == "assistant":
            msgs.pop(0)
        while len(msgs) > 2 and sum(len(m["content"]) for m in msgs) > max_chars:
            del msgs[:2]
        if msgs and msgs[0]["role"] == "assistant":
            msgs.pop(0)
        return msgs

    def delete_session(self, session_id: int) -> bool:
        with self._lock:
            # cascade에 의존하지 않는 2계층 삭제 — FK PRAGMA가 꺼진 커넥션으로 열려도 고아 메시지 없음
            self._conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            cur = self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def export_markdown(self, session_id: int) -> str:
        with self._lock:
            _, title, created, _ = self._require(session_id)
            messages = self._messages(session_id)
        lines = [
            f"# [대화기록] {title}",
            "> 이 문서는 llmsearch가 생성한 답변 기록입니다 — 1차 출처가 아닙니다. 원 출처는 각 답변 하단 목록을 확인하세요.",
            f"- 생성: {created} · 내보내기: {_now()}",
            "",
        ]
        n = 0
        for m in messages:
            if m["role"] == "user":
                n += 1
                lines.append(f"## Q{n}. {m['content']}")
                label = filters_label(m["filters"])
                if label:
                    lines.append(f"(필터: {label})")
            else:
                lines += ["", m["content"], ""]
                if m["sources"]:
                    lines.append("출처:")
                    lines += [f"- [{s.get('source_type', '')}] {s.get('title', '')} — {s.get('url_or_path', '')}"
                              for s in m["sources"]]
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def close(self) -> None:
        with self._lock:
            self._conn.close()
