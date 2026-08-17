from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_JIRA_BROWSE = re.compile(r"/browse/([A-Z][A-Z0-9]+-\d+)")
_CONF_PAGES = re.compile(r"/pages/(\d+)(?:/|$)")


def parse_atlassian_url(url: str) -> dict | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.scheme.startswith("http"):
        return None

    m = _JIRA_BROWSE.search(parsed.path)
    if m:
        return {"kind": "jira_issue", "key": m.group(1), "url": url}

    qs = parse_qs(parsed.query)
    if "pageId" in qs and qs["pageId"] and qs["pageId"][0].isdigit():
        return {"kind": "confluence_page", "page_id": qs["pageId"][0], "url": url}

    m = _CONF_PAGES.search(parsed.path)
    if m:
        return {"kind": "confluence_page", "page_id": m.group(1), "url": url}
    return None
