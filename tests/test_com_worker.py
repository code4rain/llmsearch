import threading

import pytest
from llmsearch.outlook.com_worker import ComWorker


def test_submit_runs_on_single_dedicated_thread():
    w = ComWorker()
    try:
        t1 = w.submit(lambda: threading.current_thread().name)
        t2 = w.submit(lambda: threading.current_thread().name)
        assert t1 == t2 == "com-worker"
        assert t1 != threading.current_thread().name
    finally:
        w.shutdown()


def test_submit_returns_value_and_propagates_exception():
    w = ComWorker()
    try:
        assert w.submit(lambda a, b: a + b, 1, 2) == 3
        with pytest.raises(ValueError, match="boom"):
            w.submit(lambda: (_ for _ in ()).throw(ValueError("boom")))
        # 예외 후에도 워커는 살아 있어야 함
        assert w.submit(lambda: "ok") == "ok"
    finally:
        w.shutdown()


def test_shutdown_then_submit_raises():
    w = ComWorker()
    w.shutdown()
    with pytest.raises(RuntimeError):
        w.submit(lambda: 1)
