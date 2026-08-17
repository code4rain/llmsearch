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


def test_mail_in_window_matches_filter_mail_boundaries():
    """FINDING 2: _mail_in_window is the single predicate shared by _filter_mail and
    ComOutlookClient.list_mail's inline pre-filter. Verify it reproduces the exact
    since-exclusive/until-inclusive semantics _filter_mail already guarantees.
    """
    from llmsearch.outlook.com_client import _mail_in_window

    since = datetime(2026, 8, 15, 10, 0, 0)
    until = datetime(2026, 8, 15, 11, 0, 0)
    assert _mail_in_window(datetime(2026, 8, 15, 9, 59, 59), since, until) is False  # before since
    assert _mail_in_window(since, since, until) is False  # at since — excluded
    assert _mail_in_window(datetime(2026, 8, 15, 10, 30, 0), since, until) is True  # between
    assert _mail_in_window(until, since, until) is True  # at until — included
    assert _mail_in_window(datetime(2026, 8, 15, 11, 0, 1), since, until) is False  # after until
    # No until: open-ended window
    assert _mail_in_window(datetime(2030, 1, 1), since, None) is True


def test_list_mail_early_break_equals_filter_then_truncate():
    """FINDING 2 structural guarantee: simulate the list_mail loop shape (inline
    _mail_in_window pre-filter + break once `limit` reached) over a synthetic item
    stream and confirm it yields the same result as the old approach of realizing
    everything then calling _filter_mail(...)[:limit].

    ComOutlookClient itself requires win32com (Windows-only, cannot run under WSL),
    so this test exercises the identical control-flow shape using plain dicts to
    prove the two strategies are equivalent — the real method differs only in that
    `item` is a COM object and `_mail_dict(item, folder)` reads `item.Body`.
    """
    from llmsearch.outlook.com_client import _filter_mail, _mail_in_window

    since = datetime(2026, 8, 15, 10, 0, 0)
    until = datetime(2026, 8, 15, 20, 0, 0)
    # Ascending by received_at, as Outlook Sort("[ReceivedTime]", False) guarantees.
    stream = [
        {"received_at": datetime(2026, 8, 15, 9, 0, 0), "subject": "before_since"},
        {"received_at": datetime(2026, 8, 15, 10, 30, 0), "subject": "m1"},
        {"received_at": datetime(2026, 8, 15, 11, 0, 0), "subject": "m2"},
        {"received_at": datetime(2026, 8, 15, 12, 0, 0), "subject": "m3"},
        {"received_at": datetime(2026, 8, 15, 21, 0, 0), "subject": "after_until"},
        {"received_at": datetime(2026, 8, 15, 13, 0, 0), "subject": "m4_never_reached"},
    ]

    new_reads = []  # tracks which items would trigger the expensive Body read

    def realize(mail, tracker):  # stand-in for self._mail_dict(item, folder) reading item.Body
        tracker.append(mail["subject"])
        return dict(mail)

    limit = 2
    out: list[dict] = []
    for mail in stream:
        if not _mail_in_window(mail["received_at"], since, until):
            continue
        out.append(realize(mail, new_reads))
        if limit is not None and len(out) >= limit:
            break
    filtered = _filter_mail(out, since, until)
    result = filtered[:limit] if limit is not None else filtered

    # Early break must avoid realizing (reading Body for) items past the limit.
    assert new_reads == ["m1", "m2"]

    # Old behavior (no early break): realize everything, filter, then truncate.
    old_reads = []
    old_result = _filter_mail([realize(m, old_reads) for m in stream], since, until)[:limit]

    assert [m["subject"] for m in result] == [m["subject"] for m in old_result] == ["m1", "m2"]
    assert old_reads == [m["subject"] for m in stream]  # old approach reads every item's body


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
