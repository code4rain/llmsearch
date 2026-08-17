import httpx
import pytest
from llmsearch.atlassian.auth import AtlassianAuth
from llmsearch.atlassian.http_client import HttpAtlassianClient

CONF = "https://wiki.corp.com"
JIRA = "https://jira.corp.com"

PAGE_JSON = {
    "id": "123", "title": "설계 문서", "type": "page",
    "space": {"key": "ENG"},
    "version": {"number": 4, "when": "2026-08-01T10:00:00.000+09:00"},
    "ancestors": [{"title": "루트"}, {"title": "중간"}],
    "body": {"storage": {"value": "<p>본문</p>"}},
    "_links": {"webui": "/pages/viewpage.action?pageId=123"},
}
ISSUE_JSON = {
    "key": "PROJ-1",
    "fields": {
        "summary": "버그", "description": "설명", "updated": "2026-08-02T09:00:00.000+09:00",
        "status": {"name": "Open"}, "assignee": {"displayName": "김철수"},
        "comment": {"comments": [{"author": {"displayName": "박영희"},
                                  "created": "2026-08-02T10:00:00.000+09:00", "body": "확인"}]},
    },
}


def make_client(handler, auth=None):
    return HttpAtlassianClient(
        CONF, JIRA, auth or AtlassianAuth(mode="pat", token="tok"),
        transport=httpx.MockTransport(handler),
    )


def test_get_page_maps_contract():
    def handler(request):
        assert request.url.path == "/rest/api/content/123"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json=PAGE_JSON)

    page = make_client(handler).get_page("123")
    assert page["id"] == "123" and page["space"] == "ENG" and page["version"] == 4
    assert page["ancestors"] == ["루트", "중간"]
    assert page["html"] == "<p>본문</p>"
    assert page["url"].startswith(CONF)
    assert page["updated"].startswith("2026-08-01T10:00:00")


def test_get_page_404_keyerror():
    with pytest.raises(KeyError):
        make_client(lambda r: httpx.Response(404, json={})).get_page("9")


def test_child_page_ids_paged():
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        start = int(request.url.params.get("start", 0))
        if start == 0:
            return httpx.Response(200, json={"results": [{"id": "2"}, {"id": "3"}], "size": 2, "limit": 2})
        return httpx.Response(200, json={"results": [{"id": "4"}], "size": 1, "limit": 2})

    ids = make_client(handler).child_page_ids("1")
    assert ids == ["2", "3", "4"]
    assert len(calls) == 2  # limit만큼 찼으면 다음 페이지 요청


def test_get_issue_maps_contract():
    def handler(request):
        assert request.url.path == "/rest/api/2/issue/PROJ-1"
        return httpx.Response(200, json=ISSUE_JSON)

    issue = make_client(handler).get_issue("PROJ-1")
    assert issue["summary"] == "버그" and issue["status"] == "Open"
    assert issue["assignee"] == "김철수"
    assert issue["comments"][0]["author"] == "박영희"
    assert issue["url"] == f"{JIRA}/browse/PROJ-1"


def test_get_issue_null_fields():
    lean = {"key": "P-2", "fields": {"summary": "s", "description": None, "updated": "2026-08-01T00:00:00.000+09:00",
                                     "status": {"name": "Done"}, "assignee": None, "comment": None}}
    issue = make_client(lambda r: httpx.Response(200, json=lean)).get_issue("P-2")
    assert issue["description"] == "" and issue["assignee"] == "" and issue["comments"] == []


def test_check_auth_true_false():
    ok = make_client(lambda r: httpx.Response(200, json={"name": "kim"}))
    assert ok.check_auth() is True
    bad = make_client(lambda r: httpx.Response(401, json={}))
    assert bad.check_auth() is False


def test_check_auth_confluence_only():
    """Jira base 미설정 시 Confluence space 엔드포인트로 진단해야 한다."""
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    c = HttpAtlassianClient(CONF, "", AtlassianAuth(mode="pat", token="t"),
                            transport=httpx.MockTransport(handler))
    assert c.check_auth() is True
    assert paths == ["/rest/api/space"]


def test_check_auth_requires_all_configured_services():
    """두 base가 모두 설정됐으면 둘 다 프로브해 전부 성공해야 True다 — 한쪽만 만료돼도
    False (단일 자격증명·단일 프로브의 2-서버 부정합 방지, 스펙 §7.2)."""
    def handler(request):
        if "/rest/api/space" in request.url.path:
            return httpx.Response(401, json={})  # confluence 인증 만료
        return httpx.Response(200, json={"name": "kim"})  # jira는 여전히 유효

    c = make_client(handler)  # CONF, JIRA 둘 다 설정됨
    assert c.check_auth() is False


def test_auth_headers_basic_and_cookie():
    seen = {}

    def handler(request):
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"name": "x"})

    make_client(handler, AtlassianAuth(mode="cookie", cookie="JSESSIONID=abc")).check_auth()
    assert seen["cookie"] == "JSESSIONID=abc"
    make_client(handler, AtlassianAuth(mode="basic", user="u", password="p")).check_auth()
    assert seen["authorization"].startswith("Basic ")


def test_child_page_ids_hard_cap_misbehaving_server():
    """서버가 start를 무시하고 계속 같은 페이지를 반환해도 무한루프 방지."""
    call_count = [0]

    def handler(request):
        # start 파라미터를 무시하고 항상 같은 전체 크기 페이지 반환
        call_count[0] += 1
        return httpx.Response(200, json={
            "results": [{"id": f"child-{i}"} for i in range(10)],
            "size": 10,
            "limit": 10
        })

    ids = make_client(handler).child_page_ids("1")
    # _MAX_CHILD_PAGES = 20이고 limit = 10이므로, 최대 20 * 10 = 200개
    assert len(ids) == 200
    # 20번 호출해야 하드 캡에 도달
    assert call_count[0] == 20
