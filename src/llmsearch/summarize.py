from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol

MAX_SUMMARY_INPUT_CHARS = 30000

# Windows에서 파일/폴더명에 금지된 문자(\/:*?"<>|) + 제어문자
_INVALID_SEGMENT_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_SEGMENT_LEN = 80
# Windows 예약 디바이스명 — 대소문자 무관, 확장자 없이 정확히 일치할 때만 대상 (스펙 §7.2 보안)
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


@dataclass
class SummaryResult:
    markdown: str
    category: str  # PARA 경로: "Projects/x" | "Areas/x" | "Resources/x"


def _sanitize_segment(name: str) -> str:
    """카테고리 경로의 폴더명 한 조각을 파일시스템에 안전한 단일 세그먼트로 정리한다.

    LLM이 반환한 분류명을 그대로 파일시스템 경로에 쓰면, 안에 섞인 구분자(\\, /)나
    Windows 금지 문자(:*?"<>|) 때문에 의도치 않은 하위 폴더가 생기거나 파일 생성이
    실패해 동기화 전체가 중단될 수 있다 (스펙 §7.1 P0).

    - \\/:*?"<>| 및 제어문자는 공백으로 치환한다 — 경로 구분자(\\, /)도 폴더 계층을
      새로 만들지 않도록 여기서 함께 제거한다. 예: "a/b" → "a b" (단일 세그먼트 유지).
    - 연속 공백은 하나로 모으고 양끝 공백을 제거한다.
    - 80자로 자른다.
    - 순수 점(.)으로만 구성되면("..", ".") 경로 탈출/자기참조로 해석될 수 있으므로 "_"로
      대체한다 (스펙 §7.2 P0 — Confluence ancestor 제목이 ".."일 때 미러 경로가 mirror_dir
      밖으로 탈출하는 것을 막는 방어 1계층).
    - 후행 점·공백은 제거한다 (Windows는 파일/폴더명 끝의 점·공백을 금지).
    - Windows 예약 디바이스명(CON, PRN, AUX, NUL, COM1-9, LPT1-9, 대소문자 무관)이면 앞에
      "_"를 붙인다.
    - 결과가 빈 문자열이면 "일반"으로 대체한다.
    """
    cleaned = _INVALID_SEGMENT_CHARS.sub(" ", name)
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned[:_MAX_SEGMENT_LEN].strip()
    if cleaned and set(cleaned) <= {"."}:
        cleaned = "_"
    else:
        cleaned = cleaned.rstrip(". ")
    if cleaned.upper() in _RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned or "일반"


def resolve_category(raw: str, projects: list[str], areas: list[str]) -> str:
    """LLM 분류 출력을 검증한다: 닫힌 목록 밖의 Projects/Areas는 Resources로 강등 (스펙 §7.1 P0).

    반환값의 name 부분은 항상 `_sanitize_segment`로 살균된 단일 경로 세그먼트다 —
    파일시스템 경로로 그대로 쓰여도 안전하다.
    """
    raw = raw.strip().strip("/")
    parts = raw.split("/", 1)
    if len(parts) != 2 or not parts[1]:
        return "Resources/일반"
    top, raw_name = parts
    name = _sanitize_segment(raw_name)
    if top == "Projects" and name in projects:
        return f"{top}/{name}"
    if top == "Areas" and name in areas:
        return f"{top}/{name}"
    if top in ("Projects", "Areas"):
        return f"Resources/{name}"
    if top in ("Resources", "Archives"):
        return f"{top}/{name}"
    return "Resources/일반"


def _extract_category(text: str, projects: list[str], areas: list[str]) -> tuple[str, str]:
    """LLM 출력에서 마지막 CATEGORY: 줄을 파싱·검증하고 마크다운에서 제거한다.

    Returns: (markdown_without_category_line, category)
    - CATEGORY 줄이 없으면: (text_unchanged, "Resources/일반")
    - 여러 줄 있으면: 마지막 한 줄만 파싱하고 마크다운에서 제거
    - 잘못된 접두사(예: "Category: ..."): CATEGORY 줄로 인식 안 함
    """
    category = "Resources/일반"
    out = text
    lines = text.strip().splitlines()
    for line in reversed(lines):
        if line.startswith("CATEGORY:"):
            raw_category = line.removeprefix("CATEGORY:").strip()
            category = resolve_category(raw_category, projects, areas)
            # 마크다운에서 CATEGORY 줄 제거
            out = out[: out.rfind(line)].rstrip()
            break
    return out, category


class Summarizer(Protocol):
    def summarize_and_classify(
        self, title: str, text: str, projects: list[str], areas: list[str],
        existing_resources: list[str], prior_category: str | None, glossary: str, rules: str,
    ) -> SummaryResult: ...

    def describe_filename(self, filename: str) -> str: ...

    def describe_images(self, title: str, images: list[bytes]) -> str: ...


class FakeSummarizer:
    """결정적 요약·분류 — 테스트용. 제목/본문에 프로젝트·영역명이 있으면 그리로 분류."""

    def summarize_and_classify(self, title, text, projects, areas, existing_resources,
                               prior_category, glossary, rules) -> SummaryResult:
        md = f"# {title}\n\n## 요약\n{text[:200]}\n\n## 예상 질문\n- {title}은 무엇인가?\n\n## 키워드\n{title}\n"
        if prior_category:
            return SummaryResult(md, prior_category)
        haystack = title + " " + text
        for p in projects:
            if p in haystack:
                return SummaryResult(md, f"Projects/{p}")
        for a in areas:
            if a in haystack:
                return SummaryResult(md, f"Areas/{a}")
        return SummaryResult(md, "Resources/일반")

    def describe_filename(self, filename: str) -> str:
        stem = filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")
        return f"파일명 기반 추정: {stem} 관련 문서 (내용 미인덱싱)"

    def describe_images(self, title: str, images: list[bytes]) -> str:
        return f"슬라이드 {len(images)}장 비전 설명: {title}"


_SUMMARY_PROMPT = """당신은 사내 문서 요약가다. 아래 문서를 검색에 최적화된 Markdown으로 요약하라.

반드시 이 구조를 따를 것:
# <문서 제목>
## 요약
(핵심 내용 5~10문장. 수치·날짜·고유명사 보존)
## 예상 질문
(이 문서로 답할 수 있는 질문 5개, 불릿)
## 키워드
(핵심 키워드·사람·프로젝트명, 쉼표 구분)

그리고 마지막 줄에 분류를 정확히 한 줄로 출력하라:
CATEGORY: <분류>

분류 규칙: 아래 활성 목록 중 가장 맞는 곳을 고른다. 어디에도 안 맞으면 Resources/<주제> 형식으로 새 주제를 만든다.
- 활성 프로젝트: {projects}
- 지속 영역(Areas): {areas}
- 기존 Resources 주제: {resources}
{prior}
{glossary}
{rules}

--- 문서 제목: {title} ---
{text}
"""


class GeminiSummarizer:
    def __init__(self, model: str = "gemini-flash-latest"):
        from google import genai  # 지연 import

        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY 환경변수를 설정하세요 (.env)")
        self.client = genai.Client(api_key=key)
        self.model = model

    def _generate(self, prompt: str) -> str:
        try:
            resp = self.client.models.generate_content(model=self.model, contents=prompt)
            return resp.text or ""
        except Exception:
            return ""

    def summarize_and_classify(self, title, text, projects, areas, existing_resources,
                               prior_category, glossary, rules) -> SummaryResult:
        prompt = _SUMMARY_PROMPT.format(
            projects=", ".join(projects) or "(없음)",
            areas=", ".join(areas) or "(없음)",
            resources=", ".join(existing_resources) or "(없음)",
            prior=f"- 이 문서의 기존 분류: {prior_category} (특별한 이유 없으면 유지)" if prior_category else "",
            glossary=f"\n## 용어집\n{glossary}" if glossary else "",
            rules=f"\n## 분류 규칙\n{rules}" if rules else "",
            title=title,
            text=text[:MAX_SUMMARY_INPUT_CHARS],
        )
        out = self._generate(prompt)
        out, category = _extract_category(out, projects, areas)
        return SummaryResult(out, category)

    def describe_filename(self, filename: str) -> str:
        prompt = (
            "다음 파일명만 보고 이 문서가 무엇일지 2~3문장으로 추정 설명하라. "
            "검색 키워드가 될 고유명사를 보존하라.\n파일명: " + filename
        )
        return self._generate(prompt)

    def describe_images(self, title: str, images: list[bytes]) -> str:
        from google.genai import types  # 지연 import

        parts = [types.Part.from_bytes(data=img, mime_type="image/png") for img in images]
        prompt = (
            f"다음은 사내 발표자료 '{title}'의 슬라이드 이미지다. 각 슬라이드의 핵심 내용을 "
            "검색 가능한 텍스트로 설명하라. 수치·날짜·고유명사(사람/프로젝트명)를 보존하고, "
            "슬라이드별 불릿 1~3개로 요약하라."
        )
        try:
            resp = self.client.models.generate_content(model=self.model, contents=parts + [prompt])
            return resp.text or ""
        except Exception:
            return ""
