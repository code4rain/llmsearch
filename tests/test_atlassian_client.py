import pytest
from llmsearch.atlassian.client import FakeAtlassianClient


def page(pid, title="문서", version=1):
    return {"id": pid, "space": "ENG", "title": title, "html": "<p>본문</p>",
            "version": version, "updated": "2026-08-01T10:00:00",
            "ancestors": [], "url": f"https://wiki/pages/{pid}"}


def test_get_page_and_children():
    c = FakeAtlassianClient(pages={"1": page("1"), "2": page("2", "자식")},
                            children={"1": ["2"]})
    assert c.get_page("1")["title"] == "문서"
    assert c.child_page_ids("1") == ["2"]
    assert c.child_page_ids("2") == []


def test_missing_page_raises_keyerror():
    with pytest.raises(KeyError):
        FakeAtlassianClient().get_page("999")


def test_get_issue():
    issue = {"key": "PROJ-1", "summary": "요약", "description": "설명", "status": "Open",
             "assignee": "김철수", "updated": "2026-08-02T09:00:00",
             "url": "https://jira/browse/PROJ-1",
             "comments": [{"author": "박영희", "created": "2026-08-02T10:00:00", "body": "댓글"}]}
    c = FakeAtlassianClient(issues={"PROJ-1": issue})
    assert c.get_issue("PROJ-1")["summary"] == "요약"
    with pytest.raises(KeyError):
        c.get_issue("PROJ-2")


def test_auth_flag():
    assert FakeAtlassianClient(auth_ok=False).check_auth() is False
    assert FakeAtlassianClient().check_auth() is True
