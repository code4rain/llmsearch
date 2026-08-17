from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


@dataclass
class SummaryResult:
    markdown: str
    category: str  # PARA 경로: "Projects/x" | "Areas/x" | "Resources/x"


def resolve_category(raw: str, projects: list[str], areas: list[str]) -> str:
    """LLM 분류 출력을 검증한다: 닫힌 목록 밖의 Projects/Areas는 Resources로 강등 (스펙 §7.1 P0)."""
    raw = raw.strip().strip("/")
    parts = raw.split("/", 1)
    if len(parts) != 2 or not parts[1]:
        return "Resources/일반"
    top, name = parts
    if top == "Projects" and name in projects:
        return raw
    if top == "Areas" and name in areas:
        return raw
    if top in ("Projects", "Areas"):
        return f"Resources/{name}"
    if top in ("Resources", "Archives"):
        return raw
    return "Resources/일반"


class Summarizer(Protocol):
    def summarize_and_classify(
        self, title: str, text: str, projects: list[str], areas: list[str],
        existing_resources: list[str], prior_category: str | None, glossary: str, rules: str,
    ) -> SummaryResult: ...

    def describe_filename(self, filename: str) -> str: ...


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

        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = model

    def _generate(self, prompt: str) -> str:
        resp = self.client.models.generate_content(model=self.model, contents=prompt)
        return resp.text or ""

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
            text=text[:30000],  # 프롬프트 상한 — 초과분은 요약 대상에서 절단
        )
        out = self._generate(prompt)
        category = "Resources/일반"
        lines = out.strip().splitlines()
        for line in reversed(lines):
            if line.startswith("CATEGORY:"):
                category = resolve_category(line.removeprefix("CATEGORY:"), projects, areas)
                out = out[: out.rfind(line)].rstrip()
                break
        return SummaryResult(out, category)

    def describe_filename(self, filename: str) -> str:
        prompt = (
            "다음 파일명만 보고 이 문서가 무엇일지 2~3문장으로 추정 설명하라. "
            "검색 키워드가 될 고유명사를 보존하라.\n파일명: " + filename
        )
        return self._generate(prompt)
