from __future__ import annotations

import re

# Separator lines — cut standalone (unambiguous)
_SEPARATOR_LINE = re.compile(
    r"^-{2,}\s*(Original Message|Forwarded message|원본 메일|전달된 메일)\s*-{2,}",
    re.IGNORECASE,
)

# Header lines that require corroboration (not standalone prose)
_HEADER_MARKERS = [
    re.compile(r"^From:\s*.+@"),
    re.compile(r"^보낸 사람\s?:\s*.+"),
    re.compile(r"^발신\s?:\s*.+"),
]

# Corroborating headers — proves header block context (스펙 §7.4)
_CORROBORATION_MARKERS = [
    re.compile(r"^To:\s*", re.IGNORECASE),
    re.compile(r"^받는 사람\s?:\s*"),
    re.compile(r"^수신\s?:\s*"),
    re.compile(r"^Subject:\s*", re.IGNORECASE),
    re.compile(r"^제목\s?:\s*"),
    re.compile(r"^Date:\s*", re.IGNORECASE),
    re.compile(r"^날짜\s?:\s*"),
    re.compile(r"^보낸 날짜\s?:\s*"),
    re.compile(r"^Sent:\s*", re.IGNORECASE),
    re.compile(r"^Cc:\s*", re.IGNORECASE),
    re.compile(r"^참조\s?:\s*"),
]

# Timestamp markers — sufficiently specific, cut without corroboration
_TIMESTAMP_MARKERS = [
    re.compile(r"^On .+ wrote:\s*$"),
    re.compile(r"^\d{4}년 .+님이 작성:\s*$"),
]

# Signature marker (RFC 3676 convention; 과소 절단이 과잉 절단보다 낫다는 제약에서는 보수적)
_SIGNATURE_MARKER = re.compile(r"^--\s*$")


def _has_corroboration(lines: list[str], start_idx: int, max_lookahead: int = 3) -> bool:
    """
    Check if a header line is part of a genuine email header block.
    Corroboration = at least one more header line within next lines.
    """
    for i in range(start_idx + 1, min(start_idx + 1 + max_lookahead, len(lines))):
        stripped = lines[i].strip()
        if not stripped:  # Skip blank lines
            continue
        # Found corroborating header
        if any(m.match(stripped) for m in _CORROBORATION_MARKERS):
            return True
        # Stop searching at first non-header line
        if not any(m.match(stripped) for m in _HEADER_MARKERS + _CORROBORATION_MARKERS):
            break
    return False


def _is_quote_tail(lines: list[str], start_idx: int) -> bool:
    """
    Check if all remaining non-blank lines from start_idx begin with '>'.
    Isolated '> 강조' mid-body must not truncate trailing content.
    """
    for i in range(start_idx, len(lines)):
        stripped = lines[i].strip()
        if not stripped:  # Blank lines are acceptable in quote blocks
            continue
        if not stripped.startswith(">"):
            return False
    return True


def clean_mail_body(body: str, max_chars: int = 20000) -> str:
    """
    Remove quoted text, signatures, and enforce length limit.

    Rules (순서대로 적용):
    1. `--` signature marker (RFC 3676)
    2. Separator lines (---- Original Message ----, ---- Forwarded message ----)
    3. Timestamp lines (On ... wrote:, YYYY년 ...님이 작성:) — sufficiently specific
    4. Header lines (From:, 보낸 사람:) + corroboration check
    5. Quote lines (>) only if entire tail is quoted (진짜 인용 꼬리)
    """
    lines = body.splitlines()
    cut = len(lines)

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Signature marker — intentional conservative cutoff (RFC 3676)
        if _SIGNATURE_MARKER.match(stripped):
            cut = i
            break

        # Separator line — cut standalone (unambiguous)
        if _SEPARATOR_LINE.match(stripped):
            cut = i
            break

        # Timestamp markers — sufficiently specific without corroboration
        if any(m.match(stripped) for m in _TIMESTAMP_MARKERS):
            cut = i
            break

        # Header markers — require corroboration to avoid cutting prose
        if any(m.match(stripped) for m in _HEADER_MARKERS):
            if _has_corroboration(lines, i):
                cut = i
                break

        # Quote lines — only cut if entire remaining tail is quoted
        if stripped.startswith(">"):
            if _is_quote_tail(lines, i):
                cut = i
                break

    cleaned = "\n".join(lines[:cut]).strip()
    return cleaned[:max_chars]
