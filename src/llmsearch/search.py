from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .db import search_embeddings
from .embeddings import EmbeddingProvider
from .models import Hit

RRF_K = 60
CANDIDATES = 30
PER_DOC_CAP = 3
EXCERPT_CAP = 6000
_QUERY_CACHE: dict[str, list[float]] = {}


def _fts_query(query: str) -> str:
    # FTS5 특수문자 제거 후 OR 매칭 — 정확 구문보다 재현율 우선
    tokens = ["".join(ch for ch in t if ch.isalnum()) for t in query.split()]
    tokens = [t for t in tokens if t]
    return " OR ".join(f'"{t}"' for t in tokens) if tokens else '""'


def _recency_boost(updated_at: str, now: datetime) -> float:
    try:
        dt = datetime.fromisoformat(updated_at)
    except ValueError:
        return 1.0
    days = max((now - dt).days, 0)
    return 1.0 + 0.3 * max(0.0, 1.0 - days / 365)  # 1년 내 문서에 최대 +30%


def search(
    conn: sqlite3.Connection,
    embedder: EmbeddingProvider,
    query: str,
    source_filter: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sender: str | None = None,
    k: int = 12,
) -> list[Hit]:
    if query not in _QUERY_CACHE:
        if len(_QUERY_CACHE) > 512:
            _QUERY_CACHE.clear()
        _QUERY_CACHE[query] = embedder.embed([query])[0]
    qvec = _QUERY_CACHE[query]

    vec_hits = search_embeddings(conn, qvec, CANDIDATES)          # [(chunk_id, dist)]
    fts_rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
        (_fts_query(query), CANDIDATES),
    ).fetchall()

    rrf: dict[int, float] = {}
    for rank, (cid, _) in enumerate(vec_hits):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (cid,) in enumerate(fts_rows):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    if not rrf:
        return []

    placeholders = ",".join("?" * len(rrf))
    rows = conn.execute(
        f"""SELECT c.id, c.doc_id, d.source_type, d.source_id, d.title, d.url_or_path,
                   d.updated_at, d.content_indexed, d.para_path, d.extra_json
            FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({placeholders})""",
        list(rrf),
    ).fetchall()
    # RRF 점수 내림차순 정렬 — 문서당 청크 상한 컷이 최고 점수 청크부터 채택하도록 보장
    # (ORDER BY 없는 SQL IN 절 결과는 순서를 보장하지 않아, 정렬 없이는 상한이
    #  chunk id 오름차순으로 잘려 최고 점수 청크가 누락될 수 있었다)
    rows = sorted(rows, key=lambda r: -rrf[r[0]])

    date_to_bound = date_to
    if date_to and len(date_to) == 10:  # bare YYYY-MM-DD → 해당 날짜 자정까지 포함
        date_to_bound = date_to + "T23:59:59"

    now = datetime.now()
    doc_scores: dict[int, float] = {}
    doc_meta: dict[int, tuple] = {}
    doc_best_chunk: dict[int, int] = {}
    doc_chunk_count: dict[int, int] = {}
    for cid, doc_id, stype, sid, title, url, updated, cidx, para, extra in rows:
        ex = json.loads(extra)
        if source_filter and stype not in source_filter:
            continue
        if date_from and updated < date_from:
            continue
        if date_to_bound and updated > date_to_bound:
            continue
        if sender and (ex.get("sender") or "").lower() != sender.lower():
            continue
        if doc_chunk_count.get(doc_id, 0) >= PER_DOC_CAP:
            continue
        doc_chunk_count[doc_id] = doc_chunk_count.get(doc_id, 0) + 1
        score = rrf[cid]
        if doc_id not in doc_best_chunk or score > rrf.get(doc_best_chunk[doc_id], 0):
            doc_best_chunk[doc_id] = cid
        doc_scores[doc_id] = doc_scores.get(doc_id, 0.0) + score
        doc_meta[doc_id] = (stype, sid, title, url, updated, cidx, para)

    for doc_id, (stype, sid, title, url, updated, cidx, para) in doc_meta.items():
        boost = _recency_boost(updated, now)
        if para and para.startswith("Archives/"):
            boost *= 0.5  # Archive 감쇠 — 제외가 아닌 하향 (스펙 §8 P1)
        doc_scores[doc_id] *= boost

    top = sorted(doc_scores, key=doc_scores.get, reverse=True)[:k]
    hits: list[Hit] = []
    for doc_id in top:
        stype, sid, title, url, updated, cidx, para = doc_meta[doc_id]
        chunk_rows = conn.execute(
            "SELECT id, text FROM chunks WHERE doc_id=? ORDER BY seq", (doc_id,)
        ).fetchall()
        full = "\n".join(t for _, t in chunk_rows)
        if len(full) > EXCERPT_CAP:  # 최고 청크 주변 발췌 (스펙 §8)
            best = doc_best_chunk[doc_id]
            idx = next((i for i, (c, _) in enumerate(chunk_rows) if c == best), 0)
            start = full.find(chunk_rows[idx][1])
            lo = max(0, start - EXCERPT_CAP // 2)
            full = full[lo : lo + EXCERPT_CAP]
        hits.append(Hit(stype, sid, title, url, updated, bool(cidx), doc_scores[doc_id], full))
    return hits
