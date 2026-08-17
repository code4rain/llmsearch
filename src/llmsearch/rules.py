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
    if kind == "folder" and folder is not None:
        return fnmatch.fnmatchcase(folder, pattern)
    return False


def match_override(path: str | None, sender: str | None, overrides: list[dict]) -> str | None:
    for rule in overrides:
        if _match_one(rule["match"], path, sender, None):
            return rule["target"]
    return None


def is_excluded(path: str | None, sender: str | None, folder: str | None, excludes: list[str]) -> bool:
    return any(_match_one(rule, path, sender, folder) for rule in excludes)


def load_rules_md(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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
