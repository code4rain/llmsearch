from llmsearch.atlassian.htmlmd import html_to_markdown


def test_headings_and_lists():
    md = html_to_markdown("<h1>제목</h1><ul><li>항목1</li><li>항목2</li></ul>")
    assert "제목" in md and "항목1" in md and "항목2" in md
    assert md.count("\n\n\n") == 0  # 빈 줄 압축


def test_strips_script_and_style():
    md = html_to_markdown("<p>본문</p><script>alert(1)</script><style>.x{}</style>")
    assert "본문" in md and "alert" not in md and ".x" not in md


def test_table_preserved_as_text():
    md = html_to_markdown("<table><tr><th>이름</th></tr><tr><td>김철수</td></tr></table>")
    assert "이름" in md and "김철수" in md


def test_empty():
    assert html_to_markdown("") == ""


def test_literal_script_in_cdata_preserved():
    """코드 예제 속 문자 그대로의 <script>는 삭제되면 안 된다 (markdownify가 파싱 계층에서 실제 요소만 제거)."""
    html = "<p>예제:</p><ac:plain-text-body><![CDATA[<script>alert('example');</script>]]></ac:plain-text-body><p>참고하세요.</p>"
    md = html_to_markdown(html)
    assert "example" in md and "참고하세요" in md
