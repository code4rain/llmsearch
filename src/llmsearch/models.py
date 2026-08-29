from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Document:
    """모든 커넥터가 인덱서에 넘기는 공용 문서 표현 (스펙 §6)."""

    source_type: str        # "notes" | "local_docs" | (M2+) "outlook_mail" ...
    source_id: str          # 소스 내 고유 ID (파일 절대경로 등)
    title: str
    text: str               # 인덱싱 대상 본문 (DRM 문서는 메타데이터 설명문)
    url_or_path: str        # 원본 열기용
    updated_at: datetime
    content_indexed: bool = True  # False = DRM 등으로 메타데이터만 인덱싱됨
    extra: dict = field(default_factory=dict)


@dataclass
class SyncResult:
    documents: list[Document]
    deleted_ids: list[str]  # 소스에서 사라진 source_id 목록
    state: dict             # 다음 동기화에 넘길 커서 (커넥터 자유 형식)


@dataclass
class Hit:
    """검색 결과 1건 — 문서 단위로 승격된 상태 (스펙 §8)."""

    source_type: str
    source_id: str
    title: str
    url_or_path: str
    updated_at: str         # ISO 문자열
    content_indexed: bool
    score: float
    excerpt: str            # 승격된 문서 본문 (상한 6000자)
    snippet: str = ""       # 최고 점수 청크 발췌 (헤더 제거, 200자) — 출처 카드 표시용, LLM 컨텍스트 아님
