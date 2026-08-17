from pathlib import Path

from llmsearch.atlassian.client import FakeAtlassianClient
from llmsearch.connectors.jira import sync_jira


def issue(key, summary="버그 수정", updated="2026-08-02T09:00:00", comments=None):
    return {"key": key, "summary": summary, "description": "재현 절차...", "status": "Open",
            "assignee": "김철수", "updated": updated, "url": f"https://jira/browse/{key}",
            "comments": comments if comments is not None else [
                {"author": "박영희", "created": "2026-08-02T10:00:00", "body": "확인했습니다"}]}


def test_sync_and_mirror(tmp_path: Path):
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1")})
    r = sync_jira(c, ["PROJ-1"], {}, tmp_path)
    d = r.documents[0]
    assert d.source_id == "PROJ-1" and d.title == "[PROJ-1] 버그 수정"
    assert "재현 절차" in d.text and "확인했습니다" in d.text and "박영희" in d.text
    assert (tmp_path / "PROJ-1.md").exists()
    assert d.extra["status"] == "Open"


def test_unchanged_not_reemitted(tmp_path: Path):
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1")})
    r1 = sync_jira(c, ["PROJ-1"], {}, tmp_path)
    r2 = sync_jira(c, ["PROJ-1"], r1.state, tmp_path)
    assert r2.documents == [] and r2.deleted_ids == []


def test_updated_change_reemitted(tmp_path: Path):
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1")})
    r1 = sync_jira(c, ["PROJ-1"], {}, tmp_path)
    c.issues["PROJ-1"] = issue("PROJ-1", updated="2026-08-03T09:00:00",
                               comments=[{"author": "이민수", "created": "2026-08-03T09:00:00",
                                          "body": "수정 완료"}])
    r2 = sync_jira(c, ["PROJ-1"], r1.state, tmp_path)
    assert len(r2.documents) == 1 and "수정 완료" in r2.documents[0].text


def test_gone_issue_deleted_with_mirror(tmp_path: Path):
    """접근 실패(KeyError)와 삭제를 구분: 연속 3회 KeyError에서야 진짜 삭제로 확정한다."""
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1")})
    r1 = sync_jira(c, ["PROJ-1"], {}, tmp_path)
    mirror = tmp_path / "PROJ-1.md"
    assert mirror.exists()

    c.issues = {}  # PROJ-1 KeyError
    r2 = sync_jira(c, ["PROJ-1"], r1.state, tmp_path)
    assert r2.deleted_ids == []  # Round 1: no deletion
    assert mirror.exists()  # Mirror preserved
    assert "PROJ-1" in r2.state["updated"]  # State carried forward
    assert r2.state["misses"]["PROJ-1"] == 1

    r3 = sync_jira(c, ["PROJ-1"], r2.state, tmp_path)
    assert r3.deleted_ids == []  # Round 2: no deletion
    assert mirror.exists()
    assert "PROJ-1" in r3.state["updated"]
    assert r3.state["misses"]["PROJ-1"] == 2

    r4 = sync_jira(c, ["PROJ-1"], r3.state, tmp_path)
    assert r4.deleted_ids == ["PROJ-1"]  # Round 3: deletion confirmed
    assert not mirror.exists()


def test_deregistered_key_deleted(tmp_path: Path):
    """등록 해제된 키는 접근 실패와 관계없이 즉시 삭제된다."""
    c = FakeAtlassianClient(issues={"PROJ-1": issue("PROJ-1"), "PROJ-2": issue("PROJ-2")})
    r1 = sync_jira(c, ["PROJ-1", "PROJ-2"], {}, tmp_path)
    r2 = sync_jira(c, ["PROJ-1"], r1.state, tmp_path)  # PROJ-2 등록 해제
    assert r2.deleted_ids == ["PROJ-2"]
