from __future__ import annotations

import re

from markdownify import markdownify

_BLANKS = re.compile(r"\n{3,}")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def html_to_markdown(html: str) -> str:
    if not html.strip():
        return ""
    cleaned = _SCRIPT_STYLE.sub("", html)
    md = markdownify(cleaned, heading_style="ATX")
    return _BLANKS.sub("\n\n", md).strip()
