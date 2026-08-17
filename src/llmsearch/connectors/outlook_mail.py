from __future__ import annotations

from datetime import datetime, timedelta

from ..mailtext import clean_mail_body
from ..models import Document, SyncResult
from ..outlook.client import OutlookClient
from ..rules import is_excluded

RECONCILE_INTERVAL = timedelta(hours=24)


def backlog_hint(state: dict) -> bool:
    return bool(state.get("backlog"))


def _mail_document(m: dict) -> Document:
    body = clean_mail_body(m["body"])
    text = (
        f"보낸 사람: {m['sender_name']} <{m['sender_email']}>\n"
        f"받은 날짜: {m['received_at'].isoformat()}\n"
        f"제목: {m['subject']}\n\n{body}"
    )
    return Document(
        source_type="outlook_mail", source_id=m["entry_id"], title=m["subject"],
        text=text, url_or_path=f"outlook:{m['entry_id']}",
        updated_at=m["received_at"],
        extra={"sender": m["sender_email"], "folder": m["folder"]},
    )


def sync_outlook_mail(
    client: OutlookClient, folders: list[str], since_days: int, excludes: list[str],
    state: dict, batch_size: int = 200, now: datetime | None = None,
) -> SyncResult:
    if not client.is_available():
        raise RuntimeError("Outlook을 사용할 수 없습니다 — Windows에서 Outlook 실행 후 다시 시도하세요")
    now = now or datetime.now()
    floor = now - timedelta(days=since_days)
    cursor: dict = dict(state.get("cursor", {}))
    known: dict = {f: list(ids) for f, ids in state.get("known_ids", {}).items()}
    documents: list[Document] = []
    hit_batch_limit = False

    for folder in folders:
        since = max(
            datetime.fromisoformat(cursor[folder]) if folder in cursor else floor, floor
        )
        mails = client.list_mail(folder, since=since, limit=batch_size)
        if len(mails) >= batch_size:
            hit_batch_limit = True  # 콜드스타트 진행 중 — 다음 라운드에 이어서 (스펙 §7.4 P0)
            # 동시각 경계 보호: 커서가 exclusive(>)이므로, 가득 찬 배치의 꼬리 동시각
            # 그룹은 다음 라운드에 통째로 처리한다 (트림). 전부 동시각이면 트림 불가 —
            # batch_size 이상이 같은 초에 수신된 병리적 경우만 유실 가능(문서화된 한계).
            tail_ts = mails[-1]["received_at"]
            trimmed = [m for m in mails if m["received_at"] != tail_ts]
            if trimmed:
                mails = trimmed
        folder_known = set(known.get(folder, []))
        for m in mails:
            cursor[folder] = m["received_at"].isoformat()
            if is_excluded(None, m["sender_email"], folder, excludes):
                continue
            documents.append(_mail_document(m))
            folder_known.add(m["entry_id"])
        known[folder] = sorted(folder_known)

    # 삭제 대조: 정상 상태(배치 미포화) + 24시간 경과 시에만 (수만 통 ID 조회 비용 절약)
    deleted: list[str] = []
    last_reconcile = state.get("last_reconcile")
    due = last_reconcile is None or (now - datetime.fromisoformat(last_reconcile)) >= RECONCILE_INTERVAL
    if not hit_batch_limit and due:
        for folder in folders:
            existing = client.list_mail_ids(folder, since=floor)
            gone = [i for i in known.get(folder, []) if i not in existing]
            deleted.extend(gone)
            known[folder] = [i for i in known.get(folder, []) if i in existing]
        state_reconcile = now.isoformat()
    else:
        state_reconcile = last_reconcile

    return SyncResult(
        documents=documents, deleted_ids=deleted,
        state={"cursor": cursor, "known_ids": known,
               "last_reconcile": state_reconcile, "backlog": hit_batch_limit},
    )
