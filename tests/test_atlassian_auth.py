import pytest
from llmsearch.atlassian.auth import AtlassianAuth, diagnose, resolve_auth_candidates
from llmsearch.atlassian.client import FakeAtlassianClient


def test_resolve_order_and_presence():
    env = {"ATLASSIAN_PAT": "tok", "ATLASSIAN_USER": "kim", "ATLASSIAN_PASSWORD": "pw",
           "ATLASSIAN_COOKIE": "JSESSIONID=abc"}
    cands = resolve_auth_candidates(env)
    assert [c.mode for c in cands] == ["pat", "basic", "cookie"]  # 폴백 순서 고정


def test_resolve_partial():
    cands = resolve_auth_candidates({"ATLASSIAN_USER": "kim", "ATLASSIAN_PASSWORD": "pw"})
    assert [c.mode for c in cands] == ["basic"]
    assert resolve_auth_candidates({"ATLASSIAN_USER": "kim"}) == []  # password 없이는 불성립
    assert resolve_auth_candidates({}) == []


def test_diagnose_picks_first_working():
    calls = []

    def make_client(auth):
        calls.append(auth.mode)
        return FakeAtlassianClient(auth_ok=(auth.mode == "basic"))

    cands = [AtlassianAuth(mode="pat", token="t"),
             AtlassianAuth(mode="basic", user="u", password="p"),
             AtlassianAuth(mode="cookie", cookie="c")]
    client, auth = diagnose(cands, make_client)
    assert auth.mode == "basic"
    assert calls == ["pat", "basic"]  # cookie는 시도 안 함


def test_diagnose_all_fail_raises():
    with pytest.raises(RuntimeError, match="ATLASSIAN_"):
        diagnose([AtlassianAuth(mode="pat", token="t")],
                 lambda a: FakeAtlassianClient(auth_ok=False))


def test_diagnose_no_candidates_raises():
    with pytest.raises(RuntimeError, match="ATLASSIAN_"):
        diagnose([], lambda a: FakeAtlassianClient())


def test_diagnose_survives_client_construction_error():
    def make_client(auth):
        if auth.mode == "pat":
            raise ConnectionError("서버 접속 불가")
        return FakeAtlassianClient()

    client, auth = diagnose(
        [AtlassianAuth(mode="pat", token="t"), AtlassianAuth(mode="basic", user="u", password="p")],
        make_client,
    )
    assert auth.mode == "basic"  # 생성 예외도 다음 후보로 폴백


def test_repr_hides_secrets():
    r = repr(AtlassianAuth(mode="pat", token="SECRET_TOKEN", password="PW", cookie="CK"))
    assert "SECRET_TOKEN" not in r and "PW" not in r and "CK" not in r
    assert "pat" in r  # mode는 보임


def test_service_specific_candidates_come_first():
    env = {"CONFLUENCE_PAT": "cpat", "ATLASSIAN_PAT": "apat"}
    out = resolve_auth_candidates(env, service="confluence")
    assert [(a.mode, a.token) for a in out] == [("pat", "cpat"), ("pat", "apat")]


def test_service_falls_back_to_generic_when_no_specific():
    env = {"ATLASSIAN_USER": "u", "ATLASSIAN_PASSWORD": "p"}
    out = resolve_auth_candidates(env, service="jira")
    assert len(out) == 1 and out[0].mode == "basic" and out[0].user == "u"


def test_jira_prefix_not_used_for_confluence():
    env = {"JIRA_PAT": "jpat"}
    assert resolve_auth_candidates(env, service="confluence") == []
    assert [a.token for a in resolve_auth_candidates(env, service="jira")] == ["jpat"]


def test_no_service_keeps_legacy_behavior():
    env = {"CONFLUENCE_PAT": "cpat", "ATLASSIAN_COOKIE": "ck"}
    out = resolve_auth_candidates(env)
    assert [a.mode for a in out] == ["cookie"]  # 서비스 프리픽스는 service 지정 시에만
