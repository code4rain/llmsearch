from datetime import datetime

from llmsearch.outlook.client import FakeOutlookClient


def mail(eid, ts, subject="제목"):
    return {"entry_id": eid, "subject": subject, "body": "본문", "sender_name": "김철수",
            "sender_email": "kim@corp.com", "received_at": ts, "folder": "inbox"}


def test_list_mail_sorted_and_since_exclusive():
    c = FakeOutlookClient(mails={"inbox": [
        mail("b", datetime(2026, 8, 2)), mail("a", datetime(2026, 8, 1)), mail("c", datetime(2026, 8, 3)),
    ]})
    out = c.list_mail("inbox", since=datetime(2026, 8, 1))
    assert [m["entry_id"] for m in out] == ["b", "c"]  # since와 같은 시각은 제외, 오름차순


def test_list_mail_limit():
    c = FakeOutlookClient(mails={"inbox": [mail(str(i), datetime(2026, 8, 1, i)) for i in range(5)]})
    out = c.list_mail("inbox", since=datetime(2026, 7, 1), limit=2)
    assert len(out) == 2 and out[0]["entry_id"] == "0"


def test_list_mail_until_inclusive():
    c = FakeOutlookClient(mails={"inbox": [
        mail("a", datetime(2026, 8, 1)), mail("b", datetime(2026, 8, 2)), mail("c", datetime(2026, 8, 3)),
    ]})
    out = c.list_mail("inbox", since=datetime(2026, 7, 31), until=datetime(2026, 8, 2))
    assert [m["entry_id"] for m in out] == ["a", "b"]  # until 시각과 같은 메일 포함(inclusive), 이후 제외


def test_list_mail_ids():
    c = FakeOutlookClient(mails={"inbox": [mail("a", datetime(2026, 8, 1)), mail("b", datetime(2026, 8, 2))]})
    assert c.list_mail_ids("inbox", since=datetime(2026, 7, 31)) == {"a", "b"}
    assert c.list_mail_ids("inbox", since=datetime(2026, 8, 1)) == {"b"}


def test_unknown_folder_empty():
    assert FakeOutlookClient().list_mail("없는폴더", since=datetime(2026, 1, 1)) == []


def test_list_appointments_window_overlap():
    c = FakeOutlookClient(appointments=[
        {"entry_id": "e1", "subject": "회의", "body": "", "location": "회의실",
         "start": datetime(2026, 8, 10, 10), "end": datetime(2026, 8, 10, 11), "attendees": "나"},
        {"entry_id": "e2", "subject": "과거", "body": "", "location": "",
         "start": datetime(2026, 1, 1, 10), "end": datetime(2026, 1, 1, 11), "attendees": ""},
    ])
    out = c.list_appointments(datetime(2026, 8, 1), datetime(2026, 9, 1))
    assert [a["entry_id"] for a in out] == ["e1"]


def test_availability_flag():
    assert FakeOutlookClient(available=False).is_available() is False


def test_open_item_recorded():
    c = FakeOutlookClient()
    c.open_item("abc")
    assert c.opened == ["abc"]
