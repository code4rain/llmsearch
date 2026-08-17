from datetime import datetime, timedelta

from llmsearch.connectors.outlook_cal import sync_outlook_cal
from llmsearch.outlook.client import FakeOutlookClient

NOW = datetime(2026, 8, 17, 9, 0)


def appt(eid, start, subject="팀 미팅"):
    return {"entry_id": eid, "subject": subject, "body": "안건", "location": "회의실A",
            "start": start, "end": start + timedelta(hours=1), "attendees": "나; 김철수"}


def test_indexes_occurrences_with_dates_in_text():
    c = FakeOutlookClient(appointments=[appt("e1", NOW + timedelta(days=3))])
    r = sync_outlook_cal(c, past_days=90, future_days=180, state={}, now=NOW)
    d = r.documents[0]
    assert d.source_type == "outlook_cal"
    assert d.source_id == f"e1@{(NOW + timedelta(days=3)).isoformat()}"
    assert "2026-08-20" in d.text and "회의실A" in d.text and "김철수" in d.text


def test_recurring_occurrences_distinct_ids():
    starts = [NOW + timedelta(days=7 * i) for i in range(3)]
    c = FakeOutlookClient(appointments=[appt("weekly", s) for s in starts])
    r = sync_outlook_cal(c, 90, 180, {}, now=NOW)
    assert len({d.source_id for d in r.documents}) == 3


def test_window_shift_deletes_stale():
    c = FakeOutlookClient(appointments=[appt("e1", NOW + timedelta(days=1))])
    r1 = sync_outlook_cal(c, 90, 180, {}, now=NOW)
    # 일정이 취소됨(목록에서 사라짐)
    c.appointments = []
    r2 = sync_outlook_cal(c, 90, 180, r1.state, now=NOW)
    assert r2.deleted_ids == [r1.documents[0].source_id]


def test_unchanged_appointment_not_reemitted():
    c = FakeOutlookClient(appointments=[appt("e1", NOW + timedelta(days=1))])
    r1 = sync_outlook_cal(c, 90, 180, {}, now=NOW)
    assert len(r1.documents) == 1
    r2 = sync_outlook_cal(c, 90, 180, r1.state, now=NOW)
    # 지문 미변경 → 재방출 없음 (매 동기화 전량 재임베딩 비용 방지)
    assert r2.documents == [] and r2.deleted_ids == []


def test_changed_appointment_reemitted():
    a = appt("e1", NOW + timedelta(days=1))
    c = FakeOutlookClient(appointments=[a])
    r1 = sync_outlook_cal(c, 90, 180, {}, now=NOW)
    a["location"] = "회의실B"  # 내용 변경 → 지문 변경
    r2 = sync_outlook_cal(c, 90, 180, r1.state, now=NOW)
    assert len(r2.documents) == 1 and "회의실B" in r2.documents[0].text
