from __future__ import annotations

import hashlib
import logging
import shutil
from datetime import datetime
from pathlib import Path

from ..models import Document, SyncResult
from ..rules import is_excluded, match_override
from ..summarize import Summarizer

logger = logging.getLogger(__name__)

EXTENSIONS = {".pptx", ".xlsx", ".docx", ".pdf"}
MIN_TEXT_LEN = 50
VALID_RATIO = 0.6


def extract_text(path: Path) -> str:
    from markitdown import MarkItDown  # 지연 import — 무거운 의존성

    return MarkItDown().convert(str(path)).text_content or ""


def looks_garbled(text: str) -> bool:
    """DRM/암호화 문서 판정: 추출 텍스트가 너무 짧거나 유효 문자 비율이 낮다 (스펙 §7.1 P0)."""
    stripped = text.strip()
    if len(stripped) < MIN_TEXT_LEN:
        return True
    valid = sum(1 for ch in stripped if ch.isalnum() or ch.isspace() or ch in ".,;:!?()[]{}#*-_/%~'\"")
    return valid / len(stripped) < VALID_RATIO


def _existing_resources(summaries_dir: Path) -> list[str]:
    res = summaries_dir / "Resources"
    return sorted(p.name for p in res.iterdir() if p.is_dir()) if res.exists() else []


def _place(summaries_dir: Path, category: str, original: Path, summary_md: str,
           prior: tuple[str, str] | None) -> str:
    """요약 md와 원본 복사본을 카테고리 폴더에 기록.

    - 최종 요약 경로가 이전 요약 경로(prior)와 달라지면(카테고리 변경이든, 동일 폴더 내에서
      충돌 해시가 붙거나 빠지는 이름 변경이든) 이전 요약본·복사본을 정리한다(이동, 중복 생성 금지).
      요약본·복사본의 존재 여부는 각각 독립적으로 확인한다(하나만 남아 있어도 정리됨).
    - 동일 카테고리에 동명 파일이 이미 있으면(다른 원본에서 비롯된 것) 원본 절대경로 해시로
      파일명을 구분해 덮어쓰기·중복 생성을 방지한다 (스펙 §7.1 중복 생성 금지·정확한 이동).
    """
    target_dir = summaries_dir / category
    target_dir.mkdir(parents=True, exist_ok=True)

    candidate_summary = target_dir / (original.name + ".md")
    is_ours = bool(prior) and Path(prior[1]) == candidate_summary

    if candidate_summary.exists() and not is_ours:
        # 동명 파일 충돌: 다른 원본이 이미 이 이름을 쓰고 있음 → 해시로 구분
        suffix = "__" + hashlib.sha1(str(original.resolve()).encode()).hexdigest()[:8]
        copy_name = Path(original.name).stem + suffix + Path(original.name).suffix
        summary_path = target_dir / (copy_name + ".md")
    else:
        summary_path = candidate_summary
        copy_name = original.name

    if prior and Path(prior[1]) != summary_path:
        # 요약 경로가 바뀜(카테고리 이동 또는 동일 폴더 내 충돌 해시 부여/해제) — 이전 파일 정리.
        # 요약본·복사본 존재 여부를 각각 독립적으로 확인해 고아 방지(하나만 남아 있어도 정리됨).
        old_summary = Path(prior[1])
        if old_summary.exists():
            old_summary.unlink()
        old_copy = old_summary.parent / old_summary.name.removesuffix(".md")
        if old_copy.exists():
            old_copy.unlink()

    summary_path.write_text(summary_md, encoding="utf-8")
    copy_path = target_dir / copy_name
    if not copy_path.exists() or copy_path.stat().st_mtime < original.stat().st_mtime:
        shutil.copy2(original, copy_path)
    return str(summary_path)


def _cleanup(prior: tuple[str, str] | None) -> None:
    if not prior:
        return
    summary = Path(prior[1])
    if summary.exists():
        summary.unlink()
    stem = summary.name.removesuffix(".md")
    copy = summary.parent / stem
    if copy.exists():
        copy.unlink()


def sync_local_docs(
    folders: list[Path], excludes: list[str], overrides: list[dict],
    summarizer: Summarizer, summaries_dir: Path,
    projects: list[str], areas: list[str], glossary: str, class_rules: str,
    state: dict, prior_map: dict[str, tuple[str, str]],
) -> SyncResult:
    prev: dict[str, list] = dict(state.get("files", {}))
    seen: dict[str, list] = {}
    documents: list[Document] = []

    for folder in folders:
        if not folder.exists():
            continue
        for path in sorted(p for p in folder.rglob("*") if p.suffix.lower() in EXTENSIONS):
            sid = str(path.resolve())
            if is_excluded(sid, None, path.parent.name, excludes):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            sig = [st.st_mtime, st.st_size]
            if prev.get(sid) == sig:
                seen[sid] = sig  # 변경 없음 — 이미 처리된 파일로 유지
                continue

            prior = prior_map.get(sid)
            try:
                content_indexed = True
                try:
                    text = extract_text(path)
                    if looks_garbled(text):
                        raise ValueError("garbled")
                except Exception:
                    content_indexed = False
                    text = ""

                if content_indexed:
                    result = summarizer.summarize_and_classify(
                        title=path.name, text=text, projects=projects, areas=areas,
                        existing_resources=_existing_resources(summaries_dir),
                        prior_category=prior[0] if prior else None,
                        glossary=glossary, rules=class_rules,
                    )
                    category, body = result.category, result.markdown
                else:
                    # DRM 폴백: 파일명·메타데이터만으로 설명 생성 (스펙 §7.1 P0)
                    desc = summarizer.describe_filename(path.name)
                    category = prior[0] if prior else "Resources/미분류"
                    body = (
                        f"# {path.name}\n\n## 요약\n{desc}\n\n"
                        f"(🔒 DRM/암호화로 내용 미인덱싱 — 파일명 기반)\n\n"
                        f"## 키워드\n{path.stem.replace('_', ' ').replace('-', ' ')}\n"
                    )
                override = match_override(sid, None, overrides)
                if override:
                    category = override  # 결정적 규칙이 LLM 판단보다 우선 (스펙 §9)

                summary_path = _place(summaries_dir, category, path, body, prior)
                documents.append(
                    Document(
                        source_type="local_docs", source_id=sid, title=path.name,
                        text=body, url_or_path=sid,
                        updated_at=datetime.fromtimestamp(st.st_mtime),
                        content_indexed=content_indexed,
                        extra={"para_path": category, "summary_path": summary_path},
                    )
                )
                seen[sid] = sig
            except Exception:
                # 파일 단위 격리: 이 파일 처리가 실패해도 소스 동기화 전체가 중단되지
                # 않도록 로그만 남기고 건너뛴다. seen에 넣지 않아 다음 동기화에서 재시도된다.
                logger.exception("local_docs 동기화 실패, 파일 건너뜀: %s", sid)
                continue

    deleted = [sid for sid in prev if sid not in seen]
    for sid in deleted:
        _cleanup(prior_map.get(sid))
    return SyncResult(documents=documents, deleted_ids=deleted, state={"files": seen})
