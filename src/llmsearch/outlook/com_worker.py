"""COM 전용 워커 스레드 (스펙 §5 P0).

win32com은 COM 아파트먼트 초기화가 필요하고 스레드 친화성이 있다 — 모든 COM 접근은
이 워커의 단일 스레드에서만 일어나야 한다. FastAPI 스레드풀에서 직접 호출 금지.

BaseException 전파 의도: SystemExit/KeyboardInterrupt는 호출 스레드로 전파되며 워커는 계속 동작.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable


def _com_initialize() -> None:
    try:
        import pythoncom  # Windows 전용 — WSL에서는 no-op

        pythoncom.CoInitialize()
    except ImportError:
        pass


def _com_uninitialize() -> None:
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except ImportError:
        pass


_STOP = object()


class ComWorker:
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="com-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            try:
                _com_initialize()
            except Exception:  # noqa: BLE001 — 초기화 실패해도 워커 계속 동작
                pass
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                fn, args, kwargs, done, box = item
                try:
                    box["result"] = fn(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 — 호출자에게 그대로 전파
                    box["error"] = exc
                done.set()
        finally:
            _com_uninitialize()

    def submit(self, fn: Callable, *args, **kwargs) -> Any:
        # 재진입 감지: 워커 스레드 자체에서 호출되면 직접 실행 (데드락 방지)
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)

        with self._lock:
            if self._closed:
                raise RuntimeError("ComWorker is shut down")
            done = threading.Event()
            box: dict = {}
            self._queue.put((fn, args, kwargs, done, box))

        done.wait()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(_STOP)
        self._thread.join(timeout=5)
