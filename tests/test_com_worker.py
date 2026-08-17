import concurrent.futures
import threading

import pytest
from llmsearch.outlook import com_worker
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


def test_reentrant_submit_direct_execution():
    """워커 스레드에서 submit 호출 시 데드락 없이 직접 실행."""
    w = ComWorker()
    try:
        # 외부 스레드에서 시작, 워커 내부의 람다가 다시 submit
        # timeout을 걸어서 데드락 감지
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                lambda: w.submit(lambda: w.submit(lambda: threading.current_thread().name))
            )
            result = future.result(timeout=5)
            assert result == "com-worker"
    finally:
        w.shutdown()


def test_double_shutdown_does_not_raise():
    """shutdown() 호출 2회는 에러 발생 안 함."""
    w = ComWorker()
    w.shutdown()
    w.shutdown()  # should not raise


def test_submit_survives_init_failure(monkeypatch):
    """COM 초기화 실패해도 워커는 정상 동작."""
    # _com_initialize를 RuntimeError로 만들기
    monkeypatch.setattr(
        com_worker, "_com_initialize", lambda: (_ for _ in ()).throw(RuntimeError("init failed"))
    )

    w = ComWorker()
    try:
        # 초기화 실패했지만 submit은 작동해야 함
        assert w.submit(lambda: 1) == 1
        assert w.submit(lambda a: a * 2, 3) == 6
    finally:
        w.shutdown()
