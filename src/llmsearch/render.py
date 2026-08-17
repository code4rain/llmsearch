"""슬라이드 → PNG 렌더러 (스펙 §7.1 P1 — 이미지 위주 PPT 비전 보완).

렌더링은 Windows PowerPoint COM 전용이라 Protocol로 분리한다 — WSL 테스트는 Fake,
실동작은 scripts/check_ppt_render.py 수동 게이트로 검증한다 (M2 check_outlook 패턴).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Protocol

MAX_SLIDES = 10  # 비전 API 비용·지연 상한 — 핵심 내용은 앞쪽 슬라이드에 있다는 가정


class SlideRenderer(Protocol):
    def render(self, path: Path, max_slides: int = MAX_SLIDES) -> list[bytes]: ...


class FakeSlideRenderer:
    """테스트용 — 파일명 키로 등록된 PNG 바이트를 돌려준다."""

    def __init__(self, images: dict[str, list[bytes]] | None = None):
        self.images = images or {}
        self.calls: list[str] = []

    def render(self, path: Path, max_slides: int = MAX_SLIDES) -> list[bytes]:
        self.calls.append(str(path))
        return self.images.get(Path(path).name, [])[:max_slides]


class PowerPointRenderer:
    """PowerPoint COM으로 슬라이드를 PNG 내보내기 — ComWorker STA 스레드에서 실행.

    WSL/리눅스에서는 생성만 가능하고 render()는 win32com import에서 실패한다 —
    호출부(_augment_with_vision)가 예외를 격리하므로 동기화는 계속된다.
    PowerPoint 프로세스는 사용자가 띄워둔 세션일 수 있어 Quit()하지 않는다.
    """

    def __init__(self, worker):
        self.worker = worker

    def render(self, path: Path, max_slides: int = MAX_SLIDES) -> list[bytes]:
        return self.worker.submit(self._render_in_worker, Path(path).resolve(), max_slides)

    @staticmethod
    def _render_in_worker(path: Path, max_slides: int) -> list[bytes]:
        import win32com.client  # Windows 전용 — 지연 import

        app = win32com.client.Dispatch("PowerPoint.Application")
        app.DisplayAlerts = 1  # ppAlertsNone — 복구·암호 프롬프트 등 모달 억제. 모달이 뜨면
        # ComWorker.submit의 done.wait()가 타임아웃 없이 영구 블록되고, 이 워커를 Outlook과
        # 공유하므로 메일/일정 동기화까지 함께 동결된다 — 반드시 Open 전에 설정할 것.
        pres = app.Presentations.Open(str(path), ReadOnly=True, WithWindow=False)
        out: list[bytes] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                count = min(int(pres.Slides.Count), max_slides)
                for i in range(1, count + 1):  # COM 컬렉션은 1-기반 인덱싱이 방탄
                    png = Path(tmp) / f"slide{i}.png"
                    pres.Slides(i).Export(str(png), "PNG")
                    out.append(png.read_bytes())
        finally:
            pres.Close()
        return out
