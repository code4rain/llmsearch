from pathlib import Path
from llmsearch.rules import is_excluded, load_rules_md, match_override

OVERRIDES = [
    {"match": "path:**/경영회의/**", "target": "Areas/경영지원"},
    {"match": "sender:*@partner-x.com", "target": "Projects/파트너X협업"},
]


def test_match_override_path():
    assert match_override("/docs/2026/경영회의/1월.pptx", None, OVERRIDES) == "Areas/경영지원"


def test_match_override_sender():
    assert match_override(None, "kim@partner-x.com", OVERRIDES) == "Projects/파트너X협업"


def test_match_override_none():
    assert match_override("/docs/기타.pptx", "a@b.com", OVERRIDES) is None


def test_is_excluded_folder():
    assert is_excluded("/mail/인사평가/x.msg", None, "인사평가", ["folder:인사평가"])
    assert not is_excluded("/mail/일반/x.msg", None, "일반", ["folder:인사평가"])


def test_load_rules_md(tmp_path: Path):
    f = tmp_path / "rules.md"
    f.write_text("## 용어집\nTF-N은 차세대 TF다.\n\n## 답변 규칙\n두괄식으로.\n", encoding="utf-8")
    sections = load_rules_md(f)
    assert "TF-N" in sections["용어집"]
    assert sections["답변 규칙"] == "두괄식으로."


def test_load_rules_md_missing(tmp_path: Path):
    assert load_rules_md(tmp_path / "none.md") == {}


def test_match_override_windows_path():
    """Windows 형식 경로(백슬래시)도 POSIX 패턴과 일관되게 매칭되어야 함."""
    assert match_override(r"C:\docs\2026\경영회의\1월.pptx", None, OVERRIDES) == "Areas/경영지원"


def test_match_override_case_sensitive():
    """경로 매칭은 대소문자를 구분해야 함 (모든 플랫폼에서 일관성)."""
    overrides = [{"match": "path:**/Reports/**", "target": "Area/Reports"}]
    assert match_override("/docs/reports/x.pptx", None, overrides) is None  # "reports" != "Reports"
    assert match_override("/docs/Reports/x.pptx", None, overrides) == "Area/Reports"
