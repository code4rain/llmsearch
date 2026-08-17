from llmsearch.summarize import FakeSummarizer, resolve_category, _extract_category, _sanitize_segment


def test_fake_summarizer_classifies_to_project():
    s = FakeSummarizer()
    r = s.summarize_and_classify(
        title="프로젝트A 킥오프.pptx", text="프로젝트A 일정 논의",
        projects=["프로젝트A"], areas=["팀운영"], existing_resources=[],
        prior_category=None, glossary="", rules="",
    )
    assert r.category == "Projects/프로젝트A"
    assert "## 요약" in r.markdown


def test_fake_summarizer_falls_back_to_resources():
    s = FakeSummarizer()
    r = s.summarize_and_classify("잡담.docx", "무관한 내용", ["프로젝트A"], [], [], None, "", "")
    assert r.category == "Resources/일반"


def test_fake_prior_category_sticky():
    s = FakeSummarizer()
    r = s.summarize_and_classify("x.pptx", "내용", [], [], [], "Areas/팀운영", "", "")
    assert r.category == "Areas/팀운영"  # 기존 분류 유지 우선 (분류 안정화)


def test_resolve_category_validates_closed_list():
    assert resolve_category("Projects/프로젝트A", ["프로젝트A"], []) == "Projects/프로젝트A"
    assert resolve_category("Projects/없는프젝", ["프로젝트A"], []) == "Resources/없는프젝"
    assert resolve_category("Resources/경쟁사", [], []) == "Resources/경쟁사"
    assert resolve_category("이상한값", [], []) == "Resources/일반"


def test_resolve_category_sanitizes_name_part():
    """LLM 분류 출력의 위험 문자는 살균되어 파일시스템에 안전한 단일 세그먼트가 된다."""
    assert resolve_category("Resources/기타: 참고?", [], []) == "Resources/기타 참고"


def test_resolve_category_strips_path_separators_in_name():
    """name 부분에 경로 구분자가 섞여 있어도 폴더가 추가로 생기지 않고 단일 세그먼트로 유지된다."""
    result = resolve_category("Resources/a/b", [], [])
    assert result == "Resources/a b"
    assert result.count("/") == 1  # Top/name 딱 한 번만 — 하위 폴더가 새로 생기지 않음


def test_resolve_category_empty_after_sanitize_falls_back():
    assert resolve_category("Resources/???", [], []) == "Resources/일반"


def test_sanitize_segment_truncates_and_collapses_whitespace():
    long_name = "가" * 100
    assert len(_sanitize_segment(long_name)) == 80
    assert _sanitize_segment("여러   공백   포함") == "여러 공백 포함"
    assert _sanitize_segment("") == "일반"
    assert _sanitize_segment('bad\\/:*?"<>|name') == "bad name"


def test_sanitize_segment_dot_only_rejected():
    """경로 탈출(".."/".")로 해석될 수 있는 순수 점(.) 세그먼트는 "_"로 대체한다."""
    assert _sanitize_segment("..") == "_"
    assert _sanitize_segment(".") == "_"
    assert _sanitize_segment("...") == "_"


def test_sanitize_segment_strips_trailing_dot_and_space():
    """Windows는 파일/폴더명 끝의 점·공백을 금지한다 — 후행 점/공백은 제거한다."""
    assert _sanitize_segment("이름.") == "이름"
    assert _sanitize_segment("이름...") == "이름"
    assert _sanitize_segment("이름  ") == "이름"


def test_sanitize_segment_windows_reserved_name_prefixed():
    """Windows 예약 디바이스명은 대소문자 무관하게 앞에 "_"를 붙여 충돌을 피한다."""
    assert _sanitize_segment("CON") == "_CON"
    assert _sanitize_segment("con") == "_con"
    assert _sanitize_segment("COM1") == "_COM1"
    assert _sanitize_segment("lpt9") == "_lpt9"
    assert _sanitize_segment("NUL") == "_NUL"
    assert _sanitize_segment("일반제목") == "일반제목"  # 예약명이 아니면 그대로


def test_fake_describe_filename():
    d = FakeSummarizer().describe_filename("2026_상반기_실적보고_v3.pptx")
    assert "실적보고" in d


def test_extract_category_no_category_line():
    """CATEGORY 줄이 없으면 마크다운 변경 없고 기본값 Resources/일반"""
    text = "# 문서\n## 요약\n본문"
    md, cat = _extract_category(text, [], [])
    assert cat == "Resources/일반"
    assert md == text


def test_extract_category_valid_project():
    """유효한 Projects/프로젝트A는 파싱되고 줄이 제거됨"""
    text = "# 문서\n## 요약\n내용\nCATEGORY: Projects/프로젝트A"
    md, cat = _extract_category(text, ["프로젝트A"], [])
    assert cat == "Projects/프로젝트A"
    assert "CATEGORY:" not in md
    assert "내용" in md


def test_extract_category_multiple_lines_last_wins():
    """여러 CATEGORY 줄이 있으면 마지막 한 줄만 파싱"""
    text = "CATEGORY: Projects/잘못됨\n# 문서\n내용\nCATEGORY: Projects/프로젝트A"
    md, cat = _extract_category(text, ["프로젝트A"], [])
    assert cat == "Projects/프로젝트A"
    assert md.count("CATEGORY:") == 1  # 마지막 줄만 제거되므로 첫 줄은 남음


def test_extract_category_malformed_prefix():
    """Category: 같은 잘못된 접두사는 CATEGORY로 인식 안 함"""
    text = "# 문서\nCategory: Projects/X\n내용"
    md, cat = _extract_category(text, ["프로젝트A"], [])
    assert cat == "Resources/일반"
    assert "Category: Projects/X" in md  # 제거되지 않음


def test_extract_category_invalid_project_demoted():
    """닫힌 목록 밖의 Projects는 Resources로 강등"""
    text = "# 문서\nCATEGORY: Projects/없는프젝"
    md, cat = _extract_category(text, ["프로젝트A"], [])
    assert cat == "Resources/없는프젝"
    assert "CATEGORY:" not in md
