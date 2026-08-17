"""실제 Outlook COM 접근 (Windows 전용).

ComOutlookClient는 반드시 ComWorker 스레드에서 생성·사용해야 한다(아파트먼트 친화성).
외부에서는 ThreadedOutlookClient만 사용할 것.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .com_worker import ComWorker

_FOLDER_IDS = {"inbox": 6, "sent": 5}  # OlDefaultFolders 상수
_CALENDAR_ID = 9
_RESTRICT_FMT = "%m/%d/%Y %I:%M %p"  # Outlook Restrict가 요구하는 미국식 포맷


def _filter_mail(mails: list[dict], since: datetime, until: datetime | None = None) -> list[dict]:
    """Exact mail filtering: received_at > since AND (<= until if until given).

    Outlook Restrict truncates to minute, so COM-side window is widened by 1 minute.
    This filter applies exact boundary semantics for since-exclusive, until-inclusive.
    """
    out = []
    for mail in mails:
        received = mail["received_at"]
        if received <= since:  # Exclude since boundary
            continue
        if until is not None and received > until:  # Exclude beyond until boundary
            continue
        out.append(mail)
    return out


def _filter_appointments(appts: list[dict], window_start: datetime,
                        window_end: datetime) -> list[dict]:
    """Exact appointment filtering: end > window_start AND start < window_end (overlap).

    Outlook Restrict truncates to minute, so COM-side window is widened.
    This filter applies exact overlap semantics.
    """
    out = []
    for appt in appts:
        if appt["end"] <= window_start or appt["start"] >= window_end:
            continue
        out.append(appt)
    return out


class ComOutlookClient:
    def __init__(self):
        import win32com.client  # 지연 import — Windows + pywin32 전용

        self._app = win32com.client.Dispatch("Outlook.Application")
        self._ns = self._app.GetNamespace("MAPI")

    def _folder(self, name: str):
        """Resolve folder by name.

        For known folders (inbox, sent), use OlDefaultFolders constant.
        For custom folders, search only 1 level deep: inbox's siblings and direct children.
        """
        if name in _FOLDER_IDS:
            return self._ns.GetDefaultFolder(_FOLDER_IDS[name])
        # 커스텀 폴더: 기본 스토어의 받은편지함 형제/하위에서 이름으로 탐색 (1단계만)
        inbox = self._ns.GetDefaultFolder(6)
        for candidate in (inbox.Folders, inbox.Parent.Folders):
            for f in candidate:
                if f.Name == name:
                    return f
        raise KeyError(f"Outlook 폴더를 찾을 수 없습니다: {name}")

    def is_available(self) -> bool:
        try:
            _ = self._ns.GetDefaultFolder(6).Name
            return True
        except Exception:
            return False

    def _mail_dict(self, item, folder: str) -> dict:
        received = item.ReceivedTime
        return {
            "entry_id": item.EntryID,
            "subject": item.Subject or "(제목 없음)",
            "body": item.Body or "",
            "sender_name": getattr(item, "SenderName", "") or "",
            "sender_email": getattr(item, "SenderEmailAddress", "") or "",
            "received_at": datetime(received.year, received.month, received.day,
                                    received.hour, received.minute, received.second),
            "folder": folder,
        }

    def list_mail(self, folder: str, since: datetime, until: datetime | None = None,
                  limit: int | None = None) -> list[dict]:
        items = self._folder(folder).Items
        items.Sort("[ReceivedTime]", False)  # 오름차순
        # Widen Restrict window by 1 minute to account for minute truncation
        since_widened = since - timedelta(minutes=1)
        query = f"[ReceivedTime] > '{since_widened.strftime(_RESTRICT_FMT)}'"
        if until is not None:
            query += f" AND [ReceivedTime] <= '{until.strftime(_RESTRICT_FMT)}'"
        restricted = items.Restrict(query)
        mails: list[dict] = []
        for item in restricted:
            if getattr(item, "Class", None) != 43:  # olMail만 (회의요청 등 제외)
                continue
            mails.append(self._mail_dict(item, folder))
        # Apply exact filter BEFORE limit truncation
        filtered = _filter_mail(mails, since, until)
        if limit is not None:
            return filtered[:limit]
        return filtered

    def list_mail_ids(self, folder: str, since: datetime) -> set[str]:
        items = self._folder(folder).Items
        items.Sort("[ReceivedTime]", False)
        # Widen Restrict window by 1 minute to account for minute truncation
        since_widened = since - timedelta(minutes=1)
        restricted = items.Restrict(f"[ReceivedTime] > '{since_widened.strftime(_RESTRICT_FMT)}'")
        mails = [self._mail_dict(item, folder) for item in restricted
                 if getattr(item, "Class", None) == 43]
        # Apply exact filter
        filtered = _filter_mail(mails, since)
        return {mail["entry_id"] for mail in filtered}

    def open_item(self, entry_id: str) -> None:
        self._ns.GetItemFromID(entry_id).Display()  # Outlook 창으로 표시 (스펙 §7.4)

    def list_appointments(self, window_start: datetime, window_end: datetime) -> list[dict]:
        items = self._ns.GetDefaultFolder(_CALENDAR_ID).Items
        items.Sort("[Start]")            # IncludeRecurrences 전에 Sort 필수 (Outlook 규약)
        items.IncludeRecurrences = True  # 반복 일정 전개 — Restrict로 기간 한정 (스펙 §7.5)
        # Widen window by 1 minute to account for minute truncation in Restrict
        window_start_widened = window_start - timedelta(minutes=1)
        window_end_widened = window_end + timedelta(minutes=1)
        # Overlap semantics: start <= window_end AND end >= window_start
        query = (f"[Start] <= '{window_end_widened.strftime(_RESTRICT_FMT)}'"
                 f" AND [End] >= '{window_start_widened.strftime(_RESTRICT_FMT)}'")
        appts: list[dict] = []
        for item in items.Restrict(query):
            start, end = item.Start, item.End
            appts.append({
                "entry_id": item.EntryID,
                "subject": item.Subject or "(제목 없음)",
                "body": item.Body or "",
                "location": item.Location or "",
                "start": datetime(start.year, start.month, start.day, start.hour, start.minute, start.second),
                "end": datetime(end.year, end.month, end.day, end.hour, end.minute, end.second),
                "attendees": (getattr(item, "RequiredAttendees", "") or ""),
            })
        # Apply exact overlap filter
        return _filter_appointments(appts, window_start, window_end)


class ThreadedOutlookClient:
    """OutlookClient 구현 — 모든 호출을 ComWorker 스레드로 위임 (스펙 §5 P0)."""

    def __init__(self, worker: ComWorker, client_factory: Callable = ComOutlookClient):
        self._worker = worker
        self._factory = client_factory
        self._client = None

    def _call(self, method: str, *args, **kwargs):
        def run():
            if self._client is None:
                self._client = self._factory()  # 워커 스레드에서 생성 (아파트먼트 친화성)
            return getattr(self._client, method)(*args, **kwargs)

        return self._worker.submit(run)

    def is_available(self) -> bool:
        return self._call("is_available")

    def list_mail(self, folder, since, until=None, limit=None):
        return self._call("list_mail", folder, since, until=until, limit=limit)

    def list_mail_ids(self, folder, since):
        return self._call("list_mail_ids", folder, since)

    def list_appointments(self, window_start, window_end):
        return self._call("list_appointments", window_start, window_end)

    def open_item(self, entry_id: str) -> None:
        self._call("open_item", entry_id)
