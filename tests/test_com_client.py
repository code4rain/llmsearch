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
