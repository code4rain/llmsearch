"""Windows에서 Outlook COM 연동 수동 점검 (스펙 §12: COM은 자동 테스트 불가).

사용: (Outlook 실행 상태에서) python scripts/check_outlook.py
"""
from datetime import datetime, timedelta

from llmsearch.outlook.com_client import ThreadedOutlookClient
from llmsearch.outlook.com_worker import ComWorker

worker = ComWorker()
try:
    client = ThreadedOutlookClient(worker)
    print("가용성:", client.is_available())
    since = datetime.now() - timedelta(days=7)
    mails = client.list_mail("inbox", since=since, limit=5)
    print(f"최근 7일 받은편지함 {len(mails)}통 (최대 5):")
    for m in mails:
        print(" -", m["received_at"], m["sender_email"], "|", m["subject"][:40])
    appts = client.list_appointments(datetime.now() - timedelta(days=7),
                                     datetime.now() + timedelta(days=14))
    print(f"±기간 일정 {len(appts)}건:")
    for a in appts[:5]:
        print(" -", a["start"], "|", a["subject"][:40])
    print("OK")
finally:
    worker.shutdown()
