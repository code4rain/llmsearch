import sqlite3
import threading
from pathlib import Path

import pytest

from llmsearch import chats
from llmsearch.chats import ChatStore


def test_create_list_get_and_title_normalization(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    a = s.create_session()
    b = s.create_session("  프로젝트A   회의록   " + "x" * 100)
    assert s.get_title(a) == "새 대화"
    assert s.get_title(b) == ("프로젝트A 회의록 " + "x" * 100)[:60]
    lst = s.list_sessions()
    assert [x["id"] for x in lst] == [b, a] and lst[0]["message_count"] == 0
    got = s.get_session(a)
    assert got["id"] == a and got["title"] == "새 대화" and got["messages"] == []
    with pytest.raises(KeyError):
        s.get_session(999)
    with pytest.raises(KeyError):
        s.get_title(999)


def test_append_updates_order_and_roundtrips_sources_filters(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    a = s.create_session("A")
    b = s.create_session("B")
    assert [x["id"] for x in s.list_sessions()] == [b, a]
    s.append(a, "user", "질문", filters={"source_filter": ["notes"], "date_from": None, "date_to": None, "sender": None})
    s.append(a, "assistant", "답변", sources=[{"title": "t", "source_type": "notes", "url_or_path": "/n/a.md", "excerpt": "본문"}])
    assert [x["id"] for x in s.list_sessions()] == [a, b]  # append가 updated_at 갱신
    msgs = s.get_session(a)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["filters"]["source_filter"] == ["notes"] and msgs[0]["sources"] == []
    assert msgs[1]["sources"][0]["excerpt"] == "본문" and msgs[1]["filters"] is None
    assert s.list_sessions()[0]["message_count"] == 2
    with pytest.raises(ValueError):
        s.append(a, "system", "x")
    with pytest.raises(KeyError):
        s.append(999, "user", "x")
    s.set_title(a, "  새  제목 ")
    assert s.get_title(a) == "새 제목"
    with pytest.raises(KeyError):
        s.set_title(999, "x")


def test_history_limit_leading_user_and_char_cap(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    sid = s.create_session()
    for i in range(12):
        s.append(sid, "user", f"q{i}")
        s.append(sid, "assistant", f"a{i}", sources=[{"excerpt": "x"}])
    h = s.history(sid, limit=5)  # 마지막 5건: a9,q10,a10,q11,a11 → 선두 assistant 제거
    assert [m["content"] for m in h] == ["q10", "a10", "q11", "a11"]
    assert all(set(m) == {"role", "content"} for m in h)  # sources 제외
    h = s.history(sid, limit=6, max_chars=7)  # q9..a11(6건) → 2건씩 제거: 6→4→2 (q11,a11)
    assert [m["content"] for m in h] == ["q11", "a11"]
    orphan = s.create_session()
    s.append(orphan, "user", "u1"); s.append(orphan, "user", "u2"); s.append(orphan, "assistant", "a2")
    assert [m["role"] for m in s.history(orphan)] == ["user", "user", "assistant"]  # 연속 user 허용
    assert s.history(orphan, limit=1) == []  # 마지막 1건이 assistant → 제거 후 빈 목록
    with pytest.raises(KeyError):
        s.history(999)


def test_delete_two_layer_and_no_id_reuse(tmp_path: Path):
    path = tmp_path / "chats.db"
    s = ChatStore(path)
    a = s.create_session("A")
    s.append(a, "user", "q"); s.append(a, "assistant", "a")
    assert s.delete_session(a) is True and s.delete_session(a) is False
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0  # 메시지도 삭제됨
    raw.close()
    b = s.create_session("B")
    assert b != a and s.get_session(b)["messages"] == []  # id 재사용 없음 → 삭제된 대화 부활 불가


def test_export_markdown_format(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    sid = s.create_session("<제목>")
    s.append(sid, "user", "첫 질문", filters={"source_filter": ["notes"], "date_from": "2026-08-01", "date_to": None, "sender": None})
    s.append(sid, "assistant", "첫 답변", sources=[{"source_type": "notes", "title": "킥오프", "url_or_path": "/n/k.md", "excerpt": "..."}])
    s.append(sid, "user", "둘째 질문")
    s.append(sid, "assistant", "둘째 답변")
    s.append(sid, "user", "여러 줄\n질문입니다\n\n세 줄째")
    s.append(sid, "assistant", "셋째 답변")
    md = s.export_markdown(sid)
    lines = md.splitlines()
    assert lines[0] == "# [대화기록] <제목>" and lines[1].startswith("> 이 문서는 llmsearch가 생성한 답변 기록입니다")
    assert "## Q1. 첫 질문" in md and "(필터: 소스=notes · 기간=2026-08-01~)" in md
    assert "출처:\n- [notes] 킥오프 — /n/k.md" in md
    assert "## Q2. 둘째 질문" in md and md.index("## Q1.") < md.index("## Q2.")
    assert "(필터:" not in md.split("## Q2.")[1]  # 필터 없는 턴엔 필터 줄 없음
    # 리뷰 발견: 다중행 질문을 그대로 헤딩에 넣으면 "## Qn." 줄이 개행으로 깨진다 — 공백 정규화 필요
    assert "## Q3. 여러 줄 질문입니다 세 줄째" in md
    assert "## Q3. 여러 줄\n" not in md
    with pytest.raises(KeyError):
        s.export_markdown(999)


def test_schema_version_created_and_mismatch_rejected(tmp_path: Path):
    path = tmp_path / "chats.db"
    ChatStore(path).close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == chats.SCHEMA_VERSION
    raw.execute("UPDATE meta SET value='0' WHERE key='schema_version'"); raw.commit(); raw.close()
    with pytest.raises(RuntimeError):
        ChatStore(path)


def test_concurrent_append_is_serialized(tmp_path: Path):
    s = ChatStore(tmp_path / "chats.db")
    sid = s.create_session()

    def worker(n):
        for i in range(20):
            s.append(sid, "user", f"{n}-{i}")

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert s.list_sessions()[0]["message_count"] == 100
