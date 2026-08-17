"""인증 3단 폴백 (스펙 §7.2 P0): PAT → Basic(사번/비밀번호) → 브라우저 세션 쿠키.

자격증명은 .env(환경변수)에서만 읽는다 — config·로그 평문 금지.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping

from .client import AtlassianClient

_HELP = (
    "Atlassian 인증 실패 — .env에 다음 중 하나를 설정하세요: "
    "ATLASSIAN_PAT(권장) / ATLASSIAN_USER+ATLASSIAN_PASSWORD / ATLASSIAN_COOKIE(브라우저 세션)"
)


@dataclass
class AtlassianAuth:
    mode: str  # "pat" | "basic" | "cookie"
    token: str = ""
    user: str = ""
    password: str = ""
    cookie: str = ""


def resolve_auth_candidates(env: Mapping[str, str] | None = None) -> list[AtlassianAuth]:
    e = os.environ if env is None else env
    out: list[AtlassianAuth] = []
    if e.get("ATLASSIAN_PAT"):
        out.append(AtlassianAuth(mode="pat", token=e["ATLASSIAN_PAT"]))
    if e.get("ATLASSIAN_USER") and e.get("ATLASSIAN_PASSWORD"):
        out.append(AtlassianAuth(mode="basic", user=e["ATLASSIAN_USER"], password=e["ATLASSIAN_PASSWORD"]))
    if e.get("ATLASSIAN_COOKIE"):
        out.append(AtlassianAuth(mode="cookie", cookie=e["ATLASSIAN_COOKIE"]))
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
