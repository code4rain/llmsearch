import threading
from datetime import datetime

from llmsearch.outlook.com_worker import ComWorker


def test_module_importable_without_pywin32():
    import llmsearch.outlook.com_client  # noqa: F401 — 최상위 win32com import가 없어야 함


def test_threaded_client_delegates_on_worker_thread():
    from llmsearch.outlook.com_client import ThreadedOutlookClient

    calls = []

    class Probe:
        def is_available(self):
            calls.append(("avail", threading.current_thread().name))
            return True

        def list_mail(self, folder, since, until=None, limit=None):
            calls.append(("mail", threading.current_thread().name))
            return []

    w = ComWorker()
    try:
        c = ThreadedOutlookClient(w, client_factory=Probe)
        assert c.is_available() is True
        assert c.list_mail("inbox", since=datetime(2026, 1, 1)) == []
        assert all(thread == "com-worker" for _, thread in calls)
        # 팩토리는 1회만 (워커 스레드에서 지연 생성)
        assert c._client is not None
    finally:
        w.shutdown()


def test_filter_mail_since_exclusive():
    from llmsearch.outlook.com_client import _filter_mail

    base = datetime(2026, 8, 15, 10, 0, 0)
    mails = [
        {"received_at": base, "subject": "at_boundary"},
        {"received_at": datetime(2026, 8, 15, 10, 0, 1), "subject": "after_boundary"},
    ]
    filtered = _filter_mail(mails, since=base)
    assert len(filtered) == 1
    assert filtered[0]["subject"] == "after_boundary"


def test_filter_mail_until_inclusive():
    from llmsearch.outlook.com_client import _filter_mail

    since = datetime(2026, 8, 15, 10, 0, 0)
    until = datetime(2026, 8, 15, 11, 0, 0)
    mails = [
        {"received_at": datetime(2026, 8, 15, 9, 59, 59), "subject": "before_since"},
        {"received_at": since, "subject": "at_since"},  # Excluded by > since
        {"received_at": datetime(2026, 8, 15, 10, 30, 0), "subject": "between"},
        {"received_at": until, "subject": "at_until"},  # Included by <= until
        {"received_at": datetime(2026, 8, 15, 11, 0, 1), "subject": "after_until"},
    ]
    filtered = _filter_mail(mails, since=since, until=until)
    assert len(filtered) == 2
    assert filtered[0]["subject"] == "between"
    assert filtered[1]["subject"] == "at_until"


def test_filter_appointments_no_overlap_end_at_start():
    from llmsearch.outlook.com_client import _filter_appointments

    start = datetime(2026, 8, 15, 10, 0, 0)
    end = datetime(2026, 8, 15, 11, 0, 0)
    appts = [
        {"start": datetime(2026, 8, 15, 8, 0, 0), "end": start, "subject": "ends_at_window_start"},
        {"start": datetime(2026, 8, 15, 9, 0, 0), "end": datetime(2026, 8, 15, 10, 30, 0), "subject": "overlaps"},
    ]
    filtered = _filter_appointments(appts, start, end)
    assert len(filtered) == 1
    assert filtered[0]["subject"] == "overlaps"


def test_filter_appointments_no_overlap_start_at_end():
    from llmsearch.outlook.com_client import _filter_appointments

    start = datetime(2026, 8, 15, 10, 0, 0)
    end = datetime(2026, 8, 15, 11, 0, 0)
    appts = [
        {"start": end, "end": datetime(2026, 8, 15, 12, 0, 0), "subject": "starts_at_window_end"},
        {"start": datetime(2026, 8, 15, 10, 30, 0), "end": datetime(2026, 8, 15, 11, 30, 0), "subject": "overlaps"},
    ]
    filtered = _filter_appointments(appts, start, end)
    assert len(filtered) == 1
    assert filtered[0]["subject"] == "overlaps"
