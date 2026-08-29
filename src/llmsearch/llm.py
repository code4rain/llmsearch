from __future__ import annotations

import json
from datetime import date
from typing import Callable, Iterator, Protocol

from .models import Hit

SearchFn = Callable[..., list[Hit]]
MAX_TOOL_ROUNDS = 3  # 사전 검색 이후 Claude 추가 검색 상한 (스펙 §8)

_SEARCH_TOOL = {
    "name": "search",
    "description": (
        "사내 통합 인덱스(로컬 문서 요약, 개인 메모, Outlook 메일·일정, Confluence, Jira)를 검색한다. "
        "첫 검색 결과가 부족하거나, 다른 표현·필터로 더 찾아야 할 때 호출하라. "
        "일정·날짜 관련 질문은 date_from/date_to 필터를, 특정 발신자의 메일은 sender 필터를 사용하라."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "검색 질의"},
            "source_filter": {"type": "array", "items": {"type": "string",
                "enum": ["notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira"]},
                "description": "소스 한정(선택)"},
            "date_from": {"type": "string", "description": "YYYY-MM-DD 이후(선택)"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD 이전(선택)"},
            "sender": {"type": "string", "description": "보낸 사람 이메일 (메일 전용, 선택)"},
        },
        "required": ["query"],
    },
}


class Answerer(Protocol):
    def answer_stream(self, question: str, history: list[dict], search_fn: SearchFn,
                      filters_note: str = "") -> Iterator[dict]: ...

    def update_rules(self, sections: dict[str, str]) -> None: ...


class FakeAnswerer:
    def __init__(self):
        self.rules: dict[str, str] = {}  # 마지막 update_rules 값 — 테스트 관찰용
        self.last_filters_note = ""      # 마지막 filters_note — 테스트 관찰용
        self.last_history: list | None = None  # 마지막 answer_stream 호출의 history — 테스트 관찰용

    def update_rules(self, sections: dict[str, str]) -> None:
        self.rules = dict(sections)

    def answer_stream(self, question, history, search_fn, filters_note: str = "") -> Iterator[dict]:
        self.last_history = list(history)
        self.last_filters_note = filters_note
        hits = search_fn(question)
        if not hits:
            yield {"type": "text", "text": "관련 문서를 찾지 못했습니다."}
            yield {"type": "sources", "hits": []}
            return
        yield {"type": "text", "text": f"[1] {hits[0].title} 문서에 따르면: {hits[0].excerpt[:100]}"}
        yield {"type": "sources", "hits": hits}


def _hits_block(hits: list[Hit], offset: int = 0) -> str:
    lines = []
    for i, h in enumerate(hits, start=offset + 1):
        drm = " (🔒 내용 미인덱싱 — 파일명 기반 매칭)" if not h.content_indexed else ""
        lines.append(f"[{i}] {h.title} | {h.source_type} | {h.updated_at}{drm}\n{h.excerpt}\n")
    return "\n".join(lines) or "(검색 결과 없음)"


class ClaudeAnswerer:
    def __init__(self, model: str = "claude-opus-5", active_projects: list[str] | None = None,
                 answer_rules: str = "", glossary: str = ""):
        import anthropic  # 지연 import

        self.client = anthropic.Anthropic()
        self.model = model
        self.active_projects = active_projects or []
        self.answer_rules = answer_rules
        self.glossary = glossary

    def update_rules(self, sections: dict[str, str]) -> None:
        """설정 탭 저장 즉시 반영 — 재시작 없이 다음 답변부터 새 규칙 사용 (스펙 M6 §3)."""
        self.answer_rules = sections.get("답변 규칙", "")
        self.glossary = sections.get("용어집", "")

    def _system(self) -> str:
        parts = [
            "당신은 개인용 사내 문서 검색 비서다.",
            f"오늘 날짜: {date.today().isoformat()}",
            "규칙: 제공된 근거 문서에 없는 내용은 지어내지 말 것. 각 주장 끝에 [번호] 출처를 표기할 것. "
            "근거가 부족하면 부족하다고 말할 것. 답은 한국어, 두괄식.",
        ]
        if self.active_projects:
            parts.append("현재 진행 중 프로젝트(관련 문서 우선 판단): " + ", ".join(self.active_projects))
        if self.glossary:
            parts.append("## 용어집\n" + self.glossary)
        if self.answer_rules:
            parts.append("## 답변 규칙\n" + self.answer_rules)
        return "\n\n".join(parts)

    def answer_stream(self, question, history, search_fn, filters_note: str = "") -> Iterator[dict]:
        all_hits: list[Hit] = []
        try:
            all_hits = list(search_fn(question))  # fast path 사전 검색 (스펙 §8)
            note = f"{filters_note}\n\n" if filters_note else ""
            messages = list(history) + [{
                "role": "user",
                "content": f"질문: {question}\n\n{note}사전 검색 결과:\n{_hits_block(all_hits)}",
            }]
            rounds = 0
            while True:
                with self.client.messages.stream(
                    model=self.model, max_tokens=16000,
                    system=self._system(), tools=[_SEARCH_TOOL], messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        yield {"type": "text", "text": text}
                    response = stream.get_final_message()

                if response.stop_reason == "refusal":
                    yield {"type": "error", "message": "안전 정책으로 답변이 거부되었습니다."}
                    break
                if response.stop_reason != "tool_use" or rounds >= MAX_TOOL_ROUNDS:
                    break

                rounds += 1
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    args = block.input if isinstance(block.input, dict) else json.loads(block.input)
                    hits = search_fn(
                        args.get("query", question),
                        source_filter=args.get("source_filter"),
                        date_from=args.get("date_from"),
                        date_to=args.get("date_to"),
                        sender=args.get("sender"),
                    )
                    known = {h.source_id for h in all_hits}
                    start = len(all_hits)
                    fresh = [h for h in hits if h.source_id not in known]
                    all_hits.extend(fresh)
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": _hits_block(fresh, offset=start),
                    })
                messages.append({"role": "user", "content": tool_results})
        except Exception as exc:  # API 실패 시에도 출처는 전달 (스펙 §5 에러 처리)
            yield {"type": "error", "message": f"답변 생성 실패: {exc}"}
        yield {"type": "sources", "hits": all_hits}
