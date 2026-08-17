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
            "INSERT INTO documents(source_type, source_id, title, url_or_path, updated_at, content_indexed, para_path, extra_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (doc.source_type, doc.source_id, doc.title, doc.url_or_path,
             doc.updated_at.isoformat(), int(doc.content_indexed),
             doc.extra.get("para_path"), json.dumps(doc.extra, ensure_ascii=False)),
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
