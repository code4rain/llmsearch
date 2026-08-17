"""outlook_mail 커넥터 — 롤링 윈도 동기화.

`since_days`는 인덱스가 보존하는 기간(윈도)을 정의한다(스펙 §7.4). 매 동기화마다
`floor = now - since_days`가 재계산되므로:

- **창 밖 이탈 = 삭제 취급**: `deleted_ids`에는 소스에서 실제로 삭제된 메일뿐 아니라
  `floor` 밖으로 밀려나 더 이상 보존 대상이 아닌 메일도 포함된다(둘 다 "이 동기화가
  더 이상 인덱싱을 보증하지 않는 항목"이라는 점에서 동일하게 취급 — 인덱스 보존 범위
  정리). 호출자 입장에서 둘을 구분할 필요는 없다.
- **창 확대 시 백필**: 사용자가 `since_days`를 늘리면(예: 10 → 30) `floor`가 과거로
  이동한다. 커서는 단조 증가만 하므로 그대로 두면 새로 창에 들어온 과거 구간(예:
  15일 전 메일)이 영원히 재수집되지 않는다 — 이를 감지해 해당 폴더 커서를 리셋하고
  `floor`부터 다시 스캔한다. 이미 알려진(known_ids) 메일은 재수집돼도 문서로
  재방출되지 않는다(메일 불변 가정, 아래 루프 참고) — 중복 재임베딩 비용 없이
  백필이 저렴하다.
- **excludes는 소급 적용되지 않는다(알려진 한계)**: 이미 수집돼 known_ids에 들어간
  메일은, 이후 규칙이 추가되어도 재수집 시 이미 known이므로 exclude 검사 이전에
  스킵된다 — 즉 규칙 변경 전에 수집된 메일은 규칙 변경만으로는 인덱스에서 제거되지
  않는다(재수집을 트리거하는 창 확대나 재삭제 등 별도 동작이 있어야 함).
"""

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

    # 창 확대 백필: 이전 동기화의 floor보다 이번 floor가 더 과거면(since_days 증가),
    # 커서를 그대로 두면 새로 창에 들어온 과거 구간이 영원히 재수집되지 않는다.
    # 모든 폴더 커서를 리셋(부재 취급)해 floor부터 다시 스캔한다. known_ids는 유지되므로
    # 이미 알려진 메일은 재수집돼도 아래에서 문서로 재방출되지 않는다.
    stored_floor = state.get("floor")
    window_grew = stored_floor is not None and floor < datetime.fromisoformat(stored_floor)
    cursor: dict = {} if window_grew else dict(state.get("cursor", {}))

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
            eid = m["entry_id"]
            if eid in folder_known:
                # 메일은 불변이라 가정(수정 시 새 entry_id가 아니라 같은 entry_id로
                # 내용이 바뀌는 경우는 다루지 않는다) — 창 확대 백필로 재수집돼도 이미
                # 알려진 메일은 다시 문서화(재임베딩)하지 않는다. 커서는 계속 전진시켜야
                # 하므로 이 스킵보다 먼저 cursor를 갱신한다.
                continue
            if is_excluded(None, m["sender_email"], folder, excludes):
                continue
            documents.append(_mail_document(m))
            folder_known.add(eid)
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
        state={"cursor": cursor, "known_ids": known, "floor": floor.isoformat(),
               "last_reconcile": state_reconcile, "backlog": hit_batch_limit},
    )
