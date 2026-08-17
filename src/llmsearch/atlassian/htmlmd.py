from __future__ import annotations

import re

from markdownify import markdownify

_BLANKS = re.compile(r"\n{3,}")


def html_to_markdown(html: str) -> str:
    if not html.strip():
        return ""
    md = markdownify(html, heading_style="ATX")
    return _BLANKS.sub("\n\n", md).strip()
