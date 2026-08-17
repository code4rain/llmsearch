"""인증 3단 폴백 (스펙 §7.2 P0): PAT → Basic(사번/비밀번호) → 브라우저 세션 쿠키.

자격증명은 .env(환경변수)에서만 읽는다 — config·로그 평문 금지.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .client import AtlassianClient

_HELP = (
    "Atlassian 인증 실패 — .env에 다음 중 하나를 설정하세요: "
    "ATLASSIAN_PAT(권장) / ATLASSIAN_USER+ATLASSIAN_PASSWORD / ATLASSIAN_COOKIE(브라우저 세션). "
    "Confluence와 Jira의 자격증명이 다르면 CONFLUENCE_*/JIRA_* 프리픽스로 서비스별 설정 가능"
)


_SERVICE_PREFIXES = {"confluence": "CONFLUENCE", "jira": "JIRA"}


@dataclass
class AtlassianAuth:
    mode: str  # "pat" | "basic" | "cookie"
    token: str = field(default="", repr=False)
    user: str = ""
    password: str = field(default="", repr=False)
    cookie: str = field(default="", repr=False)


def _candidates_for_prefix(e: Mapping[str, str], prefix: str) -> list[AtlassianAuth]:
    out: list[AtlassianAuth] = []
    if e.get(f"{prefix}_PAT"):
        out.append(AtlassianAuth(mode="pat", token=e[f"{prefix}_PAT"]))
    if e.get(f"{prefix}_USER") and e.get(f"{prefix}_PASSWORD"):
        out.append(AtlassianAuth(mode="basic", user=e[f"{prefix}_USER"],
                                 password=e[f"{prefix}_PASSWORD"]))
    if e.get(f"{prefix}_COOKIE"):
        out.append(AtlassianAuth(mode="cookie", cookie=e[f"{prefix}_COOKIE"]))
    return out


def resolve_auth_candidates(env: Mapping[str, str] | None = None,
                            service: str | None = None) -> list[AtlassianAuth]:
    """3단 폴백 후보 목록 (PAT → Basic → 쿠키, 스펙 §7.2 P0).

    service("confluence"|"jira")를 주면 서비스 전용 프리픽스(CONFLUENCE_/JIRA_) 후보가
    먼저 오고 공용 ATLASSIAN_ 후보가 폴백으로 뒤따른다 — DC의 PAT·세션 쿠키는 인스턴스별
    발급이라 두 서버의 자격증명이 다를 수 있기 때문 (M3 파킹 결정의 해소).
    """
    e = os.environ if env is None else env
    out: list[AtlassianAuth] = []
    if service is not None:
        out.extend(_candidates_for_prefix(e, _SERVICE_PREFIXES[service]))
    out.extend(_candidates_for_prefix(e, "ATLASSIAN"))
    return out


def diagnose(
    candidates: list[AtlassianAuth],
    make_client: Callable[[AtlassianAuth], AtlassianClient],
) -> tuple[AtlassianClient, AtlassianAuth]:
    for auth in candidates:
        try:
            client = make_client(auth)
            if client.check_auth():
                return client, auth
        except Exception:  # 접속 실패·생성 오류도 다음 후보로 폴백
            continue
    raise RuntimeError(_HELP)
