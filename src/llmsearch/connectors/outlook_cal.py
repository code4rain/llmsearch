from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from ..models import Document, SyncResult
from ..outlook.client import OutlookClient


def _occurrence_id(a: dict) -> str:
    return f"{a['entry_id']}@{a['start'].isoformat()}"


def _appt_document(a: dict) -> Document:
    text = (
        f"일정: {a['subject']}\n"
        f"날짜: {a['start'].date().isoformat()} ({a['start'].strftime('%H:%M')}~{a['end'].strftime('%H:%M')})\n"
        f"장소: {a['location']}\n참석자: {a['attendees']}\n\n{a['body']}"
    )
    return Document(
        source_type="outlook_cal", source_id=_occurrence_id(a), title=a["subject"],
        text=text, url_or_path=f"outlook:{a['entry_id']}",
        updated_at=a["start"], extra={"location": a["location"]},
    )


def sync_outlook_cal(
    client: OutlookClient, past_days: int, future_days: int, state: dict,
    now: datetime | None = None,
) -> SyncResult:
    if not client.is_available():
        raise RuntimeError("Outlook을 사용할 수 없습니다 — Windows에서 Outlook 실행 후 다시 시도하세요")
    now = now or datetime.now()
    window_start = now - timedelta(days=past_days)
    window_end = now + timedelta(days=future_days)
    appts = client.list_appointments(window_start, window_end)  # 반복 전개는 클라이언트 책임 (기간 한정, 스펙 §7.5)
    prev_fp: dict = state.get("fingerprints", {})
    current_fp: dict[str, str] = {}
    documents = []
    for a in appts:
        doc = _appt_document(a)
        fp = hashlib.sha1(doc.text.encode("utf-8")).hexdigest()
        current_fp[doc.source_id] = fp
        if prev_fp.get(doc.source_id) != fp:  # 신규·변경분만 방출 — 전량 재임베딩 방지
            documents.append(doc)
    deleted = [i for i in prev_fp if i not in current_fp]
    return SyncResult(documents=documents, deleted_ids=deleted, state={"fingerprints": current_fp})
