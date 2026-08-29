import types
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


def test_search_tool_schema_covers_all_sources_and_sender():
    from llmsearch.llm import _SEARCH_TOOL

    props = _SEARCH_TOOL["input_schema"]["properties"]
    assert props["source_filter"]["items"]["enum"] == [
        "notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira"]
    assert props["sender"]["type"] == "string"
    assert "메일" in _SEARCH_TOOL["description"]


def test_fake_answerer_accepts_filters_note():
    a = FakeAnswerer()
    list(a.answer_stream("q", [], lambda *x, **k: [HIT], filters_note="(사용자 필터 적용: 소스=notes)"))
    assert a.last_filters_note == "(사용자 필터 적용: 소스=notes)"


class _Stream:
    def __init__(self, texts, final):
        self._texts, self._final = texts, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self._texts)

    def get_final_message(self):
        return self._final


class _Messages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        texts, final = self.responses.pop(0)
        return _Stream(texts, final)


def _tool_use_then_end(tool_input: dict):
    first = types.SimpleNamespace(stop_reason="tool_use", content=[
        types.SimpleNamespace(type="tool_use", id="t1", input=tool_input)])
    second = types.SimpleNamespace(stop_reason="end_turn", content=[])
    return [(["검색 중..."], first), (["답변 [1]"], second)]


def test_claude_tool_loop_passes_sender_and_filters_note(monkeypatch):
    import sys
    from llmsearch.llm import ClaudeAnswerer

    fake_mod = types.ModuleType("anthropic")
    messages = _Messages(_tool_use_then_end(
        {"query": "김철수 메일", "sender": "kim@corp.com", "source_filter": []}))
    fake_mod.Anthropic = lambda: types.SimpleNamespace(messages=messages)
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    calls = []

    def search_fn(query, source_filter=None, date_from=None, date_to=None, sender=None):
        calls.append((query, source_filter, date_from, date_to, sender))
        return [HIT]

    a = ClaudeAnswerer()
    events = list(a.answer_stream("김철수가 보낸 결정 사항?", [], search_fn,
                                  filters_note="(사용자 필터 적용: 소스=outlook_mail)"))
    assert calls[0] == ("김철수가 보낸 결정 사항?", None, None, None, None)     # 사전 검색: 키워드 미지정
    assert calls[1] == ("김철수 메일", [], None, None, "kim@corp.com")          # 툴 호출: sender 전달
    first_user = messages.calls[0]["messages"][0]["content"]
    assert first_user.index("(사용자 필터 적용") < first_user.index("사전 검색 결과")
    assert "".join(e["text"] for e in events if e["type"] == "text") == "검색 중...답변 [1]"
    assert len(messages.calls) == 2


def test_claude_no_note_when_empty(monkeypatch):
    import sys
    from llmsearch.llm import ClaudeAnswerer

    fake_mod = types.ModuleType("anthropic")
    messages = _Messages([(["끝"], types.SimpleNamespace(stop_reason="end_turn", content=[]))])
    fake_mod.Anthropic = lambda: types.SimpleNamespace(messages=messages)
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    list(ClaudeAnswerer().answer_stream("q", [], lambda *x, **k: [HIT]))
    assert "사용자 필터" not in messages.calls[0]["messages"][0]["content"]
