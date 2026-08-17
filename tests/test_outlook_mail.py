from datetime import datetime, timedelta

from llmsearch.connectors.outlook_mail import backlog_hint, sync_outlook_mail
from llmsearch.outlook.client import FakeOutlookClient

NOW = datetime(2026, 8, 17, 12, 0)


def mail(eid, ts, sender="kim@corp.com", folder="inbox", body="본문"):
    return {"entry_id": eid, "subject": f"제목{eid}", "body": body, "sender_name": "김철수",
            "sender_email": sender, "received_at": ts, "folder": folder}


def test_initial_sync_batched_and_resumable():
    """콜드스타트: 배치 단위 재개, 중복·누락 없음. 가득 찬 배치는 꼬리 동시각 그룹을
    트림하므로 유효 배치가 batch_size보다 작을 수 있다 — 라운드를 반복해 전량 수집 확인."""
    mails = [mail(str(i), NOW - timedelta(days=10) + timedelta(hours=i)) for i in range(5)]
    c = FakeOutlookClient(mails={"inbox": mails})
    state: dict = {}
    collected: list[str] = []
    for _ in range(10):  # 충분한 라운드 상한
        r = sync_outlook_mail(c, ["inbox"], since_days=365, excludes=[], state=state,
                              batch_size=2, now=NOW)
        ids = [d.source_id for d in r.documents]
        assert not set(ids) & set(collected)  # 중복 없음
        collected.extend(ids)
        state = r.state
        if not backlog_hint(state) and not ids:
            break
    assert set(collected) == {"0", "1", "2", "3", "4"}  # 누락 없음
    assert backlog_hint(state) is False
    # 첫 라운드는 배치 한도에 걸렸어야 함 (콜드스타트 진행 표시)
    r1 = sync_outlook_mail(FakeOutlookClient(mails={"inbox": mails}), ["inbox"], 365, [], {},
                           batch_size=2, now=NOW)
    assert backlog_hint(r1.state) is True


def test_document_shape_and_body_cleaning():
    body = "핵심 내용\n\n-----Original Message-----\n이전 메일"
    c = FakeOutlookClient(mails={"inbox": [mail("a", NOW - timedelta(days=1), body=body)]})
    r = sync_outlook_mail(c, ["inbox"], 365, [], {}, now=NOW)
    d = r.documents[0]
    assert d.source_type == "outlook_mail"
    assert d.url_or_path == "outlook:a"
    assert d.extra["sender"] == "kim@corp.com"
    assert "핵심 내용" in d.text and "이전 메일" not in d.text
    assert "kim@corp.com" in d.text  # 발신자 헤더 포함


def test_sender_and_folder_excludes():
    c = FakeOutlookClient(mails={
        "inbox": [mail("a", NOW - timedelta(days=1), sender="spam@ads.com"),
                  mail("b", NOW - timedelta(days=1, hours=1))],
        "인사평가": [mail("c", NOW - timedelta(days=1), folder="인사평가")],
    })
    r = sync_outlook_mail(c, ["inbox", "인사평가"], 365,
                          ["sender:*@ads.com", "folder:인사평가"], {}, now=NOW)
    assert {d.source_id for d in r.documents} == {"b"}


def test_reconcile_reports_deleted():
    old = NOW - timedelta(days=2)
    c = FakeOutlookClient(mails={"inbox": [mail("a", old), mail("b", old + timedelta(hours=1))]})
    r1 = sync_outlook_mail(c, ["inbox"], 365, [], {}, now=NOW)
    assert {d.source_id for d in r1.documents} == {"a", "b"}
    del c.mails["inbox"][0]  # a 삭제
    # last_reconcile을 과거로 밀어 대조 강제
    r1.state["last_reconcile"] = (NOW - timedelta(days=2)).isoformat()
    r2 = sync_outlook_mail(c, ["inbox"], 365, [], r1.state, now=NOW)
    assert r2.deleted_ids == ["a"]


def test_unavailable_client_raises():
    import pytest
    c = FakeOutlookClient(available=False)
    with pytest.raises(RuntimeError, match="Outlook"):
        sync_outlook_mail(c, ["inbox"], 365, [], {}, now=NOW)


def test_same_timestamp_at_batch_boundary_not_lost():
    ts1 = NOW - timedelta(days=1)
    ts2 = ts1 + timedelta(minutes=5)
    mails = [mail("a1", ts1), mail("a2", ts1), mail("b1", ts2), mail("b2", ts2)]
    c = FakeOutlookClient(mails={"inbox": mails})
    # batch 3: fetch [a1,a2,b1] 가득참 → 꼬리 동시각(b1) 트림 → a1,a2만 처리
    r1 = sync_outlook_mail(c, ["inbox"], 365, [], {}, batch_size=3, now=NOW)
    assert {d.source_id for d in r1.documents} == {"a1", "a2"}
    # 다음 라운드에 b1,b2 통째로 — 유실·중복 없음
    r2 = sync_outlook_mail(c, ["inbox"], 365, [], r1.state, batch_size=3, now=NOW)
    assert {d.source_id for d in r2.documents} == {"b1", "b2"}
