from llmsearch.mailtext import clean_mail_body


def test_cuts_original_message_marker():
    body = "회신 본문입니다.\n\n-----Original Message-----\nFrom: a@b.com\n이전 메일 전문"
    out = clean_mail_body(body)
    assert "회신 본문" in out and "이전 메일 전문" not in out


def test_cuts_korean_reply_header():
    body = "답장 내용.\n\n보낸 사람: 김철수 <kim@corp.com>\n받는 사람: 나\n이전 내용"
    out = clean_mail_body(body)
    assert "답장 내용" in out and "이전 내용" not in out


def test_cuts_gmail_style_quote():
    body = "본문.\n\n2026년 8월 1일 (금) 오전 10:00, 김철수님이 작성:\n> 인용문"
    out = clean_mail_body(body)
    assert "본문" in out and "인용문" not in out


def test_strips_signature():
    body = "본문 내용.\n\n--\n김철수 드림\n01x-xxxx-xxxx"
    out = clean_mail_body(body)
    assert "본문 내용" in out and "드림" not in out


def test_keeps_body_without_markers():
    assert clean_mail_body("마커 없는 짧은 본문") == "마커 없는 짧은 본문"


def test_length_cap():
    assert len(clean_mail_body("가" * 50000, max_chars=1000)) <= 1000


def test_marker_at_start_keeps_nothing_but_no_crash():
    out = clean_mail_body("-----Original Message-----\n전부 인용")
    assert out == ""


# ============================================================================
# Regression tests: corroboration requirement & quote tail validation
# ============================================================================


def test_prose_from_line_does_not_cut():
    """Prose line starting with 'From:' must NOT cut without header context."""
    body = "From: 팀 전체에게 공유드립니다.\ncontact@company.com 로 문의하세요.\n이것도 본문입니다."
    out = clean_mail_body(body)
    assert "팀 전체" in out
    assert "본문" in out


def test_prose_bandasman_line_does_not_cut():
    """Prose line with '발신:' prefix must NOT cut without header context."""
    body = "발신: 정책 변경 관련 회신 부탁드립니다.\n추가 내용\n더 본문"
    out = clean_mail_body(body)
    assert "정책 변경" in out
    assert "추가 내용" in out
    assert "더 본문" in out


def test_isolated_quote_line_does_not_truncate_tail():
    """Isolated '>' line mid-body must NOT cut if trailing content exists."""
    body = "본문 시작.\n\n> 강조 포인트\n\n계속되는 본문입니다.\n더 많은 내용"
    out = clean_mail_body(body)
    assert "본문 시작" in out
    assert "계속되는 본문" in out
    assert "더 많은 내용" in out


def test_genuine_header_block_cuts():
    """Genuine header block (with To/Subject/Date) must cut."""
    body = (
        "회신합니다.\n\n"
        "보낸 사람: alice@example.com\n"
        "받는 사람: bob@example.com\n"
        "제목: 회의 일정\n"
        "원본 메일 전문"
    )
    out = clean_mail_body(body)
    assert "회신합니다" in out
    assert "원본 메일" not in out


def test_quote_tail_entire_remaining_cuts():
    """Quote tail where ALL remaining non-blank lines start with '>' must cut."""
    body = (
        "내 의견:\n"
        "좋은 아이디어\n\n"
        "> 이전 의견 1\n"
        "> 이전 의견 2\n"
        "> 이전 의견 3"
    )
    out = clean_mail_body(body)
    assert "내 의견" in out
    assert "좋은 아이디어" in out
    assert "이전 의견" not in out


def test_forwarded_message_separator_cuts():
    """'---------- Forwarded message ----------' must cut."""
    body = (
        "전달 코멘트입니다.\n"
        "---------- Forwarded message ----------\n"
        "From: original@sender.com\n"
        "원본 메일 내용"
    )
    out = clean_mail_body(body)
    assert "전달 코멘트" in out
    assert "원본 메일" not in out


def test_cuts_outlook_standard_reply_header_block():
    """Standard Korean Outlook reply header block (보낸 사람:/보낸 날짜:/받는 사람:/제목:) must cut.

    Regression for FINDING 1: '보낸 날짜:' was not in _CORROBORATION_MARKERS, so the
    lookahead in _has_corroboration broke before reaching '받는 사람:' two lines later.
    """
    body = (
        "회신 본문입니다.\n\n"
        "보낸 사람: 김철수 <kim@corp.com>\n"
        "보낸 날짜: 2026년 8월 17일 월요일 오전 9:00\n"
        "받는 사람: 나\n"
        "제목: 회의 일정\n"
        "이전 메일 전문"
    )
    out = clean_mail_body(body)
    assert "회신 본문" in out
    assert "이전 메일 전문" not in out


def test_quote_lines_with_blanks_in_tail():
    """Quote tail with blank lines interspersed must still cut (blanks OK)."""
    body = (
        "마지막 내용\n\n"
        "> 인용 1\n"
        "\n"
        "> 인용 2\n"
        "\n"
        "> 인용 3"
    )
    out = clean_mail_body(body)
    assert "마지막 내용" in out
    assert "인용" not in out
