"""COM 전용 워커 스레드 (스펙 §5 P0).

win32com은 COM 아파트먼트 초기화가 필요하고 스레드 친화성이 있다 — 모든 COM 접근은
이 워커의 단일 스레드에서만 일어나야 한다. FastAPI 스레드풀에서 직접 호출 금지.
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
        self._thread = threading.Thread(target=self._run, name="com-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        _com_initialize()
        try:
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
        if not self._closed:
            self._closed = True
            self._queue.put(_STOP)
            self._thread.join(timeout=5)
