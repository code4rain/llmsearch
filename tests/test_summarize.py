from llmsearch.summarize import FakeSummarizer, resolve_category


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


def test_fake_describe_filename():
    d = FakeSummarizer().describe_filename("2026_상반기_실적보고_v3.pptx")
    assert "실적보고" in d
