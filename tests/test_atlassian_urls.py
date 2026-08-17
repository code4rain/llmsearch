from llmsearch.atlassian.urls import parse_atlassian_url


def test_jira_browse_url():
    r = parse_atlassian_url("https://jira.corp.com/browse/PROJ-123")
    assert r == {"kind": "jira_issue", "key": "PROJ-123",
                 "url": "https://jira.corp.com/browse/PROJ-123"}


def test_jira_browse_url_with_query():
    r = parse_atlassian_url("https://jira.corp.com/browse/ABC-9?filter=1")
    assert r["kind"] == "jira_issue" and r["key"] == "ABC-9"


def test_confluence_viewpage_pageid():
    r = parse_atlassian_url("https://wiki.corp.com/pages/viewpage.action?pageId=12345")
    assert r == {"kind": "confluence_page", "page_id": "12345",
                 "url": "https://wiki.corp.com/pages/viewpage.action?pageId=12345"}


def test_confluence_modern_path():
    r = parse_atlassian_url("https://wiki.corp.com/spaces/ENG/pages/98765/제목+문서")
    assert r["kind"] == "confluence_page" and r["page_id"] == "98765"


def test_unknown_url():
    assert parse_atlassian_url("https://example.com/whatever") is None
    assert parse_atlassian_url("not a url") is None
