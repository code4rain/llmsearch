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
