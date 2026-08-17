from __future__ import annotations

import re

# 인용 시작 마커 — 이 줄부터 끝까지 절단 (스펙 §7.4: 답장 스레드 중복 인덱싱 방지)
_QUOTE_MARKERS = [
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^-{3,}\s*원본 메일\s*-{3,}"),
    re.compile(r"^From:\s?.+@"),
    re.compile(r"^보낸 사람\s?:"),
    re.compile(r"^발신\s?:"),
    re.compile(r"^On .+ wrote:\s*$"),
    re.compile(r"^\d{4}년 .+님이 작성:\s*$"),
    re.compile(r"^>"),  # 인용 접두 줄
]
_SIGNATURE_MARKER = re.compile(r"^--\s*$")


def clean_mail_body(body: str, max_chars: int = 20000) -> str:
    lines = body.splitlines()
    cut = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _SIGNATURE_MARKER.match(stripped):
            cut = i
            break
        if any(m.match(stripped) for m in _QUOTE_MARKERS):
            cut = i
            break
    cleaned = "\n".join(lines[:cut]).strip()
    return cleaned[:max_chars]
