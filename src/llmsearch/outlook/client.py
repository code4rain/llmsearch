"""Outlook 접근 계약.

메일 dict: entry_id, subject, body, sender_name, sender_email, received_at(datetime), folder
일정 dict: entry_id, subject, body, location, start(datetime), end(datetime), attendees
구현체는 이 dict 계약과 정렬/필터 시맨틱을 지켜야 한다. COM 세부는 com_client.py에만 존재한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol


class OutlookClient(Protocol):
    def is_available(self) -> bool: ...

    def list_mail(self, folder: str, since: datetime, until: datetime | None = None,
                  limit: int | None = None) -> list[dict]: ...

    def list_mail_ids(self, folder: str, since: datetime) -> set[str]: ...

    def list_appointments(self, window_start: datetime, window_end: datetime) -> list[dict]: ...

    def open_item(self, entry_id: str) -> None: ...


class FakeOutlookClient:
    """테스트·오프라인 개발용 — 프로토콜 시맨틱(received_at 오름차순, since exclusive)을 그대로 구현."""

    def __init__(self, mails: dict[str, list[dict]] | None = None,
                 appointments: list[dict] | None = None, available: bool = True):
        self.mails = mails or {}
        self.appointments = appointments or []
        self.available = available
        self.opened: list[str] = []

    def is_available(self) -> bool:
        return self.available

    def list_mail(self, folder: str, since: datetime, until: datetime | None = None,
                  limit: int | None = None) -> list[dict]:
        items = [m for m in self.mails.get(folder, []) if m["received_at"] > since]
        if until is not None:
            items = [m for m in items if m["received_at"] <= until]
        items.sort(key=lambda m: m["received_at"])
        return items[:limit] if limit is not None else items

    def list_mail_ids(self, folder: str, since: datetime) -> set[str]:
        return {m["entry_id"] for m in self.mails.get(folder, []) if m["received_at"] > since}

    def list_appointments(self, window_start: datetime, window_end: datetime) -> list[dict]:
        return sorted(
            (a for a in self.appointments if a["end"] > window_start and a["start"] < window_end),
            key=lambda a: a["start"],
        )

    def open_item(self, entry_id: str) -> None:
        self.opened.append(entry_id)
