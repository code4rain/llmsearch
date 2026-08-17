"""Windows 수동 게이트: PowerPoint COM 슬라이드 렌더링 실동작 확인 (M4).

사용: python scripts/check_ppt_render.py <pptx 경로>  (check_outlook과 동일하게 editable install 전제)
확인 항목: PowerPoint COM 기동, DisplayAlerts 억제 상태에서 WithWindow=False 열기,
Slide.Export PNG, 바이트 회수. POWERPNT.EXE는 Quit하지 않으므로 실행 후 상주한다 —
사용자 PowerPoint 세션을 죽이지 않기 위한 의도된 트레이드오프.
"""
import sys
from pathlib import Path

from llmsearch.outlook.com_worker import ComWorker
from llmsearch.render import PowerPointRenderer

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def main() -> None:
    if len(sys.argv) != 2:
        print("사용법: python scripts/check_ppt_render.py <pptx 경로>")
        sys.exit(1)
    worker = ComWorker()
    try:
        images = PowerPointRenderer(worker).render(Path(sys.argv[1]))
        print(f"렌더링 성공: 슬라이드 {len(images)}장")
        for i, img in enumerate(images):
            ok = "OK" if img[:8] == PNG_MAGIC else "FAIL"
            print(f"  slide{i}: {len(img)} bytes, PNG 시그니처={ok}")
    finally:
        worker.shutdown()


if __name__ == "__main__":
    main()
