from __future__ import annotations

import fnmatch
from pathlib import Path, PureWindowsPath


def _match_one(rule: str, path: str | None, sender: str | None, folder: str | None) -> bool:
    kind, _, pattern = rule.partition(":")
    if kind == "path" and path is not None:
        # 경로 구분자를 통일해 Windows/POSIX 양쪽에서 동일하게 매칭
        # PureWindowsPath로 정규화하면 \ 와 / 를 모두 /로 변환하여 모든 플랫폼에서 일관성 있음
        norm = PureWindowsPath(path).as_posix()
        return fnmatch.fnmatchcase(norm, pattern)
    if kind == "sender" and sender is not None:
        return fnmatch.fnmatch(sender.lower(), pattern.lower())
    if kind == "folder":
        # path가 있으면 경로의 모든 구성요소(폴더) 각각을 검사한다 — 직계 부모 폴더만
        # 검사하면 `folder:인사평가`가 /mail/인사평가/sub/a.md처럼 조상 폴더에 있는
        # 파일을 놓친다. path가 없을 때만 전달받은 folder(직계 부모) 인자로 폴백한다.
        if path is not None:
            parts = PureWindowsPath(path).parts
            return any(fnmatch.fnmatchcase(part, pattern) for part in parts)
        if folder is not None:
            return fnmatch.fnmatchcase(folder, pattern)
    return False


def match_override(path: str | None, sender: str | None, overrides: list[dict]) -> str | None:
    for rule in overrides:
        if _match_one(rule["match"], path, sender, None):
            return rule["target"]
    return None


def is_excluded(path: str | None, sender: str | None, folder: str | None, excludes: list[str]) -> bool:
    return any(_match_one(rule, path, sender, folder) for rule in excludes)


def parse_rules_md(text: str) -> dict[str, str]:
    """`## 섹션` 헤더 단위로 본문을 나눈다 — GUI가 저장 전 본문으로 섹션 목록을 보여줄 때도 같은 파서."""
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def load_rules_md(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_rules_md(path.read_text(encoding="utf-8"))
