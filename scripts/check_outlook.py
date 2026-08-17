"""Windows에서 Outlook COM 연동 수동 점검 (스펙 §12: COM은 자동 테스트 불가).

사용: (Outlook 실행 상태에서) python scripts/check_outlook.py

주의: Restrict 쿼리가 %m/%d/%Y 미국식 포맷을 사용하므로, 월/일이 뒤집혀 잘못
처리될 수 있다. 아래 경계 메일을 확인하여 월/일 순서가 올바른지 육안으로 검증할 것.
"""
from datetime import datetime, timedelta

from llmsearch.outlook.com_client import ThreadedOutlookClient
from llmsearch.outlook.com_worker import ComWorker

worker = ComWorker()
try:
    client = ThreadedOutlookClient(worker)
    print("가용성:", client.is_available())

    # 경계 검증: day ≤ 12인 날짜를 사용하여 month/day 순서 검증
    now = datetime.now()
    boundary_day = min(now.day, 5)  # 1-5 범위의 날짜 사용
    if now.month == 1:
        boundary_date = datetime(now.year, 1, boundary_day)
    else:
        boundary_date = datetime(now.year, now.month - 1, boundary_day)

    print(f"\n[경계 검증] {boundary_date.strftime('%Y-%m-%d')}부터의 메일:")
    boundary_mails = client.list_mail("inbox", since=boundary_date, limit=3)
    for m in boundary_mails:
        print(f"  {m['received_at']} | {m['sender_email']} | {m['subject'][:40]}")
    print("  ^ 위 received_at이 모두 경계 날짜 이후여야 함 (month/day 순서 확인)")

    since = datetime.now() - timedelta(days=7)
    mails = client.list_mail("inbox", since=since, limit=5)
    print(f"\n최근 7일 받은편지함 {len(mails)}통 (최대 5):")
    for m in mails:
        print(" -", m["received_at"], m["sender_email"], "|", m["subject"][:40])

    # FINDING 4: 조직 내부(Exchange, X.500 DN) 발신자도 SMTP 주소로 정규화됐는지 육안 확인.
    # sender_email에 '@'가 없으면 X.500 DN 폴백이 실패했거나 GetExchangeUser 경로가
    # 예상과 다르게 동작한 것 — Sender.GetExchangeUser().PrimarySmtpAddress 로직 재점검 필요.
    print("\n[EX 발신자 SMTP 확인] sender_email이 SMTP 형식(@ 포함)인지:")
    for m in mails:
        ok = "@" in m["sender_email"]
        print(f"  {'OK ' if ok else 'FAIL'} {m['sender_email'] or '(빈 값)'} | {m['subject'][:40]}")

    appts = client.list_appointments(datetime.now() - timedelta(days=7),
                                     datetime.now() + timedelta(days=14))
    print(f"\n±기간 일정 {len(appts)}건:")
    for a in appts[:5]:
        print(" -", a["start"], "|", a["subject"][:40])

    print("\nOK")
finally:
    worker.shutdown()
