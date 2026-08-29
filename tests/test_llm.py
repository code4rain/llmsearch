from datetime import datetime

from llmsearch.llm import FakeAnswerer
from llmsearch.models import Hit

HIT = Hit("notes", "a.md", "프로젝트A 킥오프", "/n/a.md", "2026-08-01", True, 1.0, "회의록 본문")


def test_fake_answerer_streams_and_cites():
    calls = []

    def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
        calls.append(query)
        return [HIT]

    events = list(FakeAnswerer().answer_stream("킥오프 언제였지?", [], search_fn))
    assert calls == ["킥오프 언제였지?"]
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "프로젝트A 킥오프" in text
    sources = [e for e in events if e["type"] == "sources"]
    assert len(sources) == 1 and sources[0]["hits"][0].source_id == "a.md"


def test_fake_answerer_no_results():
    events = list(FakeAnswerer().answer_stream("없는 질문", [], lambda *a, **k: []))
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "찾지 못" in text


def test_claude_answerer_importable_without_key():
    # 키 없이 모듈 import 가능해야 함 (지연 초기화)
    from llmsearch.llm import ClaudeAnswerer  # noqa: F401


def test_claude_answerer_presearch_exception_yields_error_and_sources(monkeypatch):
    # 사전 검색 실패 시 error + sources 이벤트 전달 확인 (계약 준수)
    from llmsearch.llm import ClaudeAnswerer

    # anthropic.Anthropic 스텁으로 API 없이 생성 가능하게
    monkeypatch.setattr("anthropic.Anthropic", lambda **k: object())

    answerer = ClaudeAnswerer()

    def failing_search_fn(*a, **k):
        raise ValueError("Search failed")

    events = list(answerer.answer_stream("질문", [], failing_search_fn))

    # error 이벤트 확인
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert "Search failed" in errors[0]["message"]

    # sources 이벤트 확인 (빈 hits)
    sources = [e for e in events if e["type"] == "sources"]
    assert len(sources) == 1
    assert sources[0]["hits"] == []


def test_fake_answerer_update_rules_keeps_last():
    from llmsearch.llm import FakeAnswerer

    a = FakeAnswerer()
    assert a.rules == {}
    a.update_rules({"답변 규칙": "두괄식", "용어집": "PJA = 프로젝트A"})
    assert a.rules["답변 규칙"] == "두괄식"


def test_claude_answerer_update_rules_changes_system_prompt(monkeypatch):
    import sys, types
    from llmsearch.llm import ClaudeAnswerer

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: object()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    a = ClaudeAnswerer(answer_rules="", glossary="")
    assert "## 답변 규칙" not in a._system()
    a.update_rules({"답변 규칙": "두괄식", "용어집": "PJA = 프로젝트A"})
    assert "## 답변 규칙\n두괄식" in a._system() and "## 용어집\nPJA = 프로젝트A" in a._system()
    a.update_rules({})
    assert "## 답변 규칙" not in a._system()
