"""API 사용량 카운터 + 일일 호출 상한 (스펙 §10 P2 — v1은 로그 출력, GUI 표시는 추후).

`usage.json`에 {"YYYY-MM-DD": {"embed": n, "summary": n, "vision": n, "answer": n}}
형태로 영속화한다. 상한(daily_limit)은 요약·인덱싱 경로에만 적용된다 — 적용 지점은
웹 계층의 run_sync 진입 게이트이고, 트래커와 카운팅 래퍼 자신은 절대 차단하지 않는다.
그래야 검색(쿼리 임베딩)·채팅 답변이 상한 도달 후에도 계속 동작한다 (스펙 §10:
"상한 도달 시 요약·인덱싱만 일시정지, 검색은 유지").
단일 앱 인스턴스를 전제한다 — 기동 시 1회 로드 후 매 record()마다 전체 dict를 덮어써서
저장하므로, 동일 파일을 공유하는 다중 프로세스 인스턴스나 실행 중 수동 편집은 카운트가
유실될 수 있다.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .embeddings import EmbeddingProvider
    from .summarize import Summarizer

logger = logging.getLogger(__name__)

_KEEP_DAYS = 30  # usage.json에 보관하는 일자 수 — 그 이전은 기록 시점에 정리


class UsageTracker:
    def __init__(self, path: Path, daily_limit: int = 0):
        self.path = path
        self.daily_limit = daily_limit  # 0 이하 = 무제한
        # chat(FastAPI 스레드풀)과 run_sync(스케줄러/수동 동기화 스레드)가 동시에 기록한다 —
        # sync_lock은 run_sync 쪽만 잡으므로 트래커가 자체 락으로 dict 변이·파일 쓰기를 직렬화.
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, int]] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or not all(isinstance(v, dict) for v in loaded.values()):
                    raise ValueError("usage.json 형태가 잘못됨: dict-of-dicts 필요")
                self._data = loaded
            except (ValueError, OSError, UnicodeDecodeError):
                logger.warning("usage.json 파싱 실패 — 카운터를 새로 시작합니다: %s", path)
                self._data = {}

    def _today(self) -> str:
        return date.today().isoformat()

    def record(self, kind: str, count: int = 1) -> None:
        if not isinstance(kind, str):
            raise TypeError(f"kind는 문자열이어야 함: {type(kind).__name__}")
        if count < 1:
            raise ValueError(f"count는 1 이상이어야 함: {count}")
        with self._lock:
            today = self._today()
            day_data = self._data.setdefault(today, {})
            day_data[kind] = day_data.get(kind, 0) + count
            for old in sorted(self._data)[:-_KEEP_DAYS]:
                del self._data[old]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_name(self.path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp_path, self.path)
            total = sum(self._data.get(today, {}).values())
        logger.info(
            "API 사용량 +%d(%s) — 오늘 누적 %d건, 일일 상한 %s",
            count, kind, total,
            self.daily_limit if self.daily_limit > 0 else "없음",
        )

    def today_total(self) -> int:
        with self._lock:
            return sum(self._data.get(self._today(), {}).values())

    def today_by_kind(self) -> dict[str, int]:
        """오늘 종류별 카운트 복사본 — 반환값을 변경해도 내부 상태에 영향 없음."""
        with self._lock:
            return dict(self._data.get(self._today(), {}))

    def indexing_allowed(self) -> bool:
        return self.daily_limit <= 0 or self.today_total() < self.daily_limit


class CountingEmbedder:
    """EmbeddingProvider 래퍼 — 배치 호출 1건당 record("embed"). 차단하지 않는다.

    기록이 위임(inner 호출)보다 먼저 일어나므로 inner가 예외를 던져도 카운트는 남는다 —
    실패한 호출도 API 예산을 소모했다고 보수적으로 취급하려는 의도다.
    """

    def __init__(self, inner: EmbeddingProvider, tracker: UsageTracker):
        self.inner = inner
        self.tracker = tracker

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.tracker.record("embed")
        return self.inner.embed(texts)


class CountingSummarizer:
    """Summarizer 래퍼 — 요약·파일명 설명은 "summary", 비전 설명은 "vision"으로 기록.

    기록이 위임(inner 호출)보다 먼저 일어나므로 inner가 예외를 던져도 카운트는 남는다 —
    실패한 호출도 API 예산을 소모했다고 보수적으로 취급하려는 의도다.
    """

    def __init__(self, inner: Summarizer, tracker: UsageTracker):
        self.inner = inner
        self.tracker = tracker

    def summarize_and_classify(self, *args, **kwargs):
        self.tracker.record("summary")
        return self.inner.summarize_and_classify(*args, **kwargs)

    def describe_filename(self, filename: str) -> str:
        self.tracker.record("summary")
        return self.inner.describe_filename(filename)

    def describe_images(self, title: str, images: list[bytes]) -> str:
        self.tracker.record("vision")
        return self.inner.describe_images(title, images)
