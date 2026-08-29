"""Playwright 브라우저 E2E — demo_server.py(127.0.0.1:8642) 대상 전체 시나리오.

사용: demo_server.py를 먼저 띄운 뒤 ./.venv/bin/python tools/e2e/verify.py
(WSL에서 chromium 라이브러리 부족 시 docs/HANDOFF.md §2의 LD_LIBRARY_PATH 방법 참조)

시나리오: 소스 6종 → Atlassian URL 등록/dedup → 6종 동기화(비전 증강 pptx 포함) →
아카이브 목록 → 채팅+출처 → 열기 버튼 → 프로젝트 완료 처리(디스크 이동 확인) →
등록 삭제 → 로그 → 사용량 카운터(usage.json) → 일일 API 호출 상한 게이트(M5) →
채팅 필터(소스 체크박스)·출처 발췌·골든 평가 GUI(M7).
"""
import json
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8642"
DATA = Path(__file__).resolve().parents[2] / ".e2e-data"
results: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))
    if not ok:
        raise AssertionError(f"{name}: {detail}")


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()

    # 1. 페이지 로드 + 소스 6종
    page.goto(BASE)
    check("페이지 로드", page.title() == "llmsearch", f"title={page.title()}")
    page.click("text=소스")
    page.wait_for_selector("#srcTable tbody tr")
    sources_text = page.locator("#srcTable tbody").inner_text()
    for src in ("notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira"):
        check(f"소스 표시: {src}", src in sources_text)

    # 2. Atlassian URL 등록 + 중복 dedup
    for url, label in (
        ("https://wiki.example.com/pages/viewpage.action?pageId=100", "page 100"),
        ("https://jira.example.com/browse/PROJ-1", "PROJ-1"),
    ):
        page.fill("#atlUrl", url)
        page.click("form >> text=등록")
        page.wait_for_timeout(400)
        check(f"등록 표시: {label}", label in page.locator("#atlList").inner_text())
    before = page.locator("#atlList li").count()
    page.fill("#atlUrl", "https://wiki.example.com/pages/viewpage.action?pageId=100&x=1")
    page.click("form >> text=등록")
    page.wait_for_timeout(400)
    check("중복 등록 dedup", page.locator("#atlList li").count() == before == 2)

    # usage.json 조회 헬퍼 — 이후 3단계(동기화 전 여유 확인)와 9~10단계(사용량 카운터·
    # 일일 상한 게이트)에서 공용으로 쓴다.
    usage_path = DATA / "data" / "usage.json"
    today_key = date.today().isoformat()
    DAILY_LIMIT = 50

    def usage_today() -> dict:
        return json.loads(usage_path.read_text(encoding="utf-8")).get(today_key, {})

    def usage_total() -> int:
        return sum(usage_today().values())

    # 2.5. 동기화 시작 전 사용량 여유 확인 — 이 시나리오는 뒤(10단계)에서 일부러 채팅을
    #      반복해 일일 상한까지 소진시킨다. 같은 usage.json을 여러 번 실행에 걸쳐 재사용하면
    #      (또는 이전 실행이 비정상 종료해 파일이 남아있으면) 이번 실행이 이미 상한 근처에서
    #      시작해 3단계 동기화 자체가 게이트에 막혀 실패할 수 있다 — 시나리오 순서와 예산이
    #      결합돼 있음을 여기서 미리 단언해 원인을 명확히 한다. 파일이 아직 없으면 통과.
    guard_exists = usage_path.exists()
    guard_total = usage_total() if guard_exists else 0
    check("동기화 시작 전 사용량 여유", not guard_exists or guard_total < DAILY_LIMIT,
          f"exists={guard_exists} total={guard_total} limit={DAILY_LIMIT}"
          " — 이전 실행 잔재로 이미 상한에 근접/도달했을 수 있음")

    # 3. 6종 동기화
    for src, expected in (("notes", "2"), ("local_docs", "1"), ("outlook_mail", "1"),
                          ("outlook_cal", "1"), ("confluence", "2"), ("jira", "1")):
        row = page.locator("#srcTable tbody tr", has_text=src).first
        row.locator("button", has_text="동기화").click()
        page.wait_for_timeout(700)
        row = page.locator("#srcTable tbody tr", has_text=src).first
        cell = row.locator("td").nth(1).inner_text()
        check(f"동기화 후 문서 수: {src}", cell == expected, f"count={cell}")

    # 4. 비전 증강 확인
    summary = DATA / "data" / "summaries" / "Projects" / "프로젝트A" / "프로젝트A_아키텍처.pptx.md"
    check("비전 증강 요약 생성", summary.exists(), str(summary))
    check("비전 설명 섹션 포함", "비전 설명" in summary.read_text(encoding="utf-8"))

    # 5. 아카이브 목록
    page.click("text=소스")
    page.wait_for_selector("#projList li")
    check("아카이브 목록: 프로젝트A", "프로젝트A" in page.locator("#projList").inner_text())

    # 6. 채팅 + 출처 + 열기(비Windows 안내)
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    page.click("nav >> text=채팅")
    page.fill("#question", "프로젝트A 킥오프 언제였지?")
    page.click("text=검색")
    page.wait_for_selector(".src", timeout=10000)
    check("채팅 답변 수신", "프로젝트A" in page.locator("#messages").inner_text())
    cards = page.locator(".src")
    check("출처 카드 표시", cards.count() >= 1, f"cards={cards.count()}")
    open_btn = cards.first.locator("button", has_text="열기")
    check("열기 버튼 존재", open_btn.count() >= 1)
    open_btn.click()
    page.wait_for_timeout(500)
    check("열기 버튼 비Windows 안내", len(dialogs) >= 1,
          dialogs[0][:60] if dialogs else "no dialog")

    # 7. 프로젝트 완료 처리 → 디스크 이동
    dialogs.clear()
    page.click("nav >> text=소스")
    page.wait_for_selector("#projList li")
    page.locator("#projList li", has_text="프로젝트A").locator(
        "button", has_text="완료 처리").click()
    page.wait_for_timeout(800)
    check("confirm+hint 다이얼로그", len(dialogs) >= 2 and any("config.yaml" in m for m in dialogs),
          " / ".join(m[:40] for m in dialogs))
    check("아카이브 후 목록 비움", page.locator("#projList li").count() == 0)
    check("디스크 이동: Archives", (DATA / "data" / "summaries" / "Archives" / "프로젝트A").is_dir())
    check("디스크 이동: Projects 제거",
          not (DATA / "data" / "summaries" / "Projects" / "프로젝트A").exists())

    # 8. 등록 삭제 + 로그
    page.locator("#atlList li", has_text="PROJ-1").locator("button", has_text="삭제").click()
    page.wait_for_timeout(400)
    check("등록 삭제", "PROJ-1" not in page.locator("#atlList").inner_text())
    page.click("nav >> text=로그")
    page.wait_for_timeout(300)
    log_text = page.locator("#logBody").inner_text()
    for src in ("notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira"):
        check(f"로그 기록: {src}", src in log_text)

    # 9. 사용량 카운터 (usage.json) — 지금까지의 6종 동기화(embed·summary·vision)와
    #    채팅 1회(embed·answer)가 이미 오늘 날짜 아래 4종 모두를 최소 1건씩 기록했어야 한다.
    #    usage_path/today_key/usage_today()/usage_total()/DAILY_LIMIT는 2.5단계에서 정의.
    check("usage.json 생성", usage_path.exists(), str(usage_path))

    today_usage = usage_today()
    for kind in ("embed", "summary", "vision", "answer"):
        check(f"사용량 기록: {kind}", today_usage.get(kind, 0) >= 1, f"count={today_usage.get(kind, 0)}")

    # 9.5 M6a — 설정 탭 rules.md 편집·재로드 (스펙 §9)
    page.click("nav >> text=설정")
    page.wait_for_selector("#rulesText")
    page.wait_for_timeout(300)
    check("설정 탭 템플릿 로드", page.input_value("#rulesText").startswith("# 규칙 (rules.md)"))
    page.fill("#rulesText", "# 규칙 (rules.md)\n\n## 용어집\nPJA = 프로젝트A\n\n## 분류 규칙\n\n"
                            "## 요약 규칙\n수치는 표로\n\n## 답변 규칙\n두괄식\n")
    page.click("#saveRulesBtn")
    page.wait_for_timeout(300)
    status = page.locator("#rulesStatus").inner_text()
    check("규칙 저장 상태", "저장됨" in status and "요약 규칙" in status, status)
    page.click("nav >> text=채팅")
    page.click("nav >> text=설정")
    page.wait_for_timeout(300)
    check("규칙 재로드 일치", "PJA = 프로젝트A" in page.input_value("#rulesText"))

    # 9.6 M6a — 사용량 한 줄 표시 (스펙 §10 GUI)
    page.click("nav >> text=소스")
    page.wait_for_selector("#usageLine")
    page.wait_for_timeout(300)
    usage_line = page.locator("#usageLine").inner_text()
    check("사용량 표시", "오늘 API 호출" in usage_line and "embed" in usage_line
          and f"/ {DAILY_LIMIT}건" in usage_line, usage_line)

    # 9.7 M6a — 출처 카드 재요약: 데모 pptx는 비전 경로라 summary +1, vision +1
    before = usage_today()
    page.click("nav >> text=채팅")
    page.fill("#question", "프로젝트A 아키텍처 표지")
    before_cards = page.locator(".src").count()  # #messages는 누적이라 이전 답변 카드가 남아 있다
    # "text=검색" 단독 셀렉터는 이미 렌더된 Jira 출처 카드 "[PROJ-1] 검색 버그 수정"과 모호해진다
    # (실측: page.click("text=검색")이 폼 버튼이 아닌 .src 카드를 클릭해 검색 자체가 발화하지
    # 않음) — form >> text= 패턴(등록 버튼과 동일)으로 폼 내부 버튼만 특정한다.
    page.click("form >> text=검색")
    page.wait_for_function(f"document.querySelectorAll('.src').length > {before_cards}", timeout=10000)
    dialogs.clear()
    page.locator(".src button", has_text="재요약").last.click()  # 방금 받은 답변의 카드; confirm은 dialog 핸들러가 accept
    page.wait_for_timeout(1500)
    after = usage_today()
    check("문서 재요약: summary +1", after.get("summary", 0) == before.get("summary", 0) + 1,
          f"{before.get('summary')}→{after.get('summary')}")
    check("문서 재요약: vision +1", after.get("vision", 0) == before.get("vision", 0) + 1,
          f"{before.get('vision')}→{after.get('vision')}")
    check("문서 재요약: 완료 alert", any("재요약 완료: 1건" in m for m in dialogs),
          " / ".join(m[:40] for m in dialogs))
    md_files = list((DATA / "data" / "summaries").rglob("프로젝트A_아키텍처*.md"))
    check("재요약 후 요약 md 중복 없음", len(md_files) == 1, str(md_files))

    # 9.8 M6a — 설정 탭 전체 재요약: confirm 문구에 건수, 실행 후 summary +1
    before = usage_today()
    dialogs.clear()
    page.click("nav >> text=설정")
    page.click("#resumAllBtn")
    page.wait_for_timeout(1500)
    check("전체 재요약 confirm 건수", any("1건을 다시 요약" in m for m in dialogs),
          " / ".join(m[:40] for m in dialogs))
    check("전체 재요약: summary +1", usage_today().get("summary", 0) == before.get("summary", 0) + 1)

    # 9.9 M6b — 인덱스 재구축: 요약 md 재사용(summary/vision 불변), 문서 수 복원 (스펙 M6 §6)
    # 9.5에서 만든 rules.md는 아직 notes 인덱스에 없다 — 먼저 반영해야 재구축 후 수가 일치한다 (notes 2→3)
    page.request.post(f"{BASE}/api/sync/notes")
    page.wait_for_timeout(300)
    before = usage_today()
    counts_before = {r["source"]: r["doc_count"] for r in page.request.get(f"{BASE}/api/sources").json()}
    dialogs.clear()
    page.click("nav >> text=설정")
    page.click("#rebuildBtn")  # confirm·alert는 dialog 핸들러가 accept
    page.wait_for_timeout(1000)
    check("재구축 시작 alert", any("재구축 시작" in m for m in dialogs), " / ".join(m[:40] for m in dialogs))
    # 재수집은 백그라운드 스레드 — /api/status를 폴링해 기다린다. (wait_for_function에 Promise를 넘기면
    # Playwright가 pending Promise를 truthy로 보고 즉시 반환하므로 쓰지 않는다.)
    status = {"rebuilding": True, "rebuild_in_progress": True}
    for _ in range(60):  # 0.5s × 60 = 30s
        status = page.request.get(f"{BASE}/api/status").json()
        if not status["rebuilding"] and not status["rebuild_in_progress"]:
            break
        page.wait_for_timeout(500)
    check("재구축 재수집 완료", not status["rebuilding"] and not status["rebuild_in_progress"], str(status))
    counts_after = {r["source"]: r["doc_count"] for r in page.request.get(f"{BASE}/api/sources").json()}
    for src in ("notes", "local_docs", "outlook_mail", "outlook_cal"):
        check(f"재구축 후 문서 수 복원: {src}", counts_after[src] == counts_before[src],
              f"{counts_before[src]}→{counts_after[src]}")
    after = usage_today()
    check("재구축: summary 불변(요약 md 재사용)", after.get("summary", 0) == before.get("summary", 0))
    check("재구축: vision 불변", after.get("vision", 0) == before.get("vision", 0))
    check("재구축: embed 증가", after.get("embed", 0) > before.get("embed", 0))
    check("재구축: 미등록 jira는 재수집 대상 아님", counts_after["jira"] == 0, f"jira={counts_after['jira']}")
    page.click("nav >> text=소스")
    page.wait_for_timeout(300)
    check("재구축 후 배너 없음", page.locator("#banner").is_hidden())

    # 9.10 M7 — 채팅 필터(notes만) → 마지막 답변 카드 전부 notes + 필터 표시 줄 + 발췌 (스펙 M7 §2·§3)
    page.click("nav >> text=채팅")
    page.click("#filters summary")
    page.check(".srcChk[data-src='notes']")
    page.fill("#question", "프로젝트A 회고 개선점")
    before_cards = page.locator(".src").count()
    page.click("form >> text=검색")
    page.wait_for_function(f"document.querySelectorAll('.src').length > {before_cards}", timeout=10000)
    page.wait_for_timeout(300)
    last = page.locator(".msg-a").last
    kinds = [t.strip("()").split(" · ")[0] for t in last.locator(".src small").all_inner_texts()]
    check("필터: 카드 전부 notes", kinds and all(k == "notes" for k in kinds), str(kinds))
    check("필터 표시 줄", "필터(첫 검색 기준): 소스=notes" in page.locator(".filters-note").last.inner_text())
    snips = last.locator(".snip").all_inner_texts()
    check("출처 카드 발췌", len(snips) >= 1 and all(s.strip() for s in snips), str(snips[:1]))
    page.uncheck(".srcChk[data-src='notes']")  # 이후 단계(10단계 UI 채팅)에 영향 없게

    # 9.11 M7 — 골든 평가 GUI: 적중 1 + 확정 미스 1 → 50% (1/2) ❌ (스펙 M7 §4)
    page.click("nav >> text=설정")
    page.wait_for_selector("#goldenText")
    page.wait_for_function("document.getElementById('goldenStatus').textContent.endsWith('건')", timeout=10000)  # loadGolden() 완료 대기
    page.fill("#goldenText", "- question: 프로젝트A 킥오프 언제?\n  expect_source_id: kickoff.md\n"
                             "- question: 존재하지 않는 주제 XYZQW\n  expect_source_id: none.md\n")
    page.click("#saveGoldenBtn")
    page.wait_for_timeout(300)
    check("골든 저장 2건", "저장됨 · 2건" in page.locator("#goldenStatus").inner_text())
    dialogs.clear()
    page.click("#runGoldenBtn")  # confirm은 dialog 핸들러가 accept
    page.wait_for_selector("#goldenResult", state="visible", timeout=10000)
    header = page.locator("#goldenHeader").inner_text()
    check("골든 결과 헤더", "50% (1/2)" in header and "❌" in header, header)
    check("골든 결과 표 2행", page.locator("#goldenTable tbody tr").count() == 2)
    check("골든 미스 표시", "❌" in page.locator("#goldenTable tbody tr").nth(1).inner_text())

    # 10. 일일 상한 게이트 — 동기화만 차단, 채팅(검색·답변)은 유지 (스펙 §10)
    #     데모 서버 daily_api_call_limit=50 — usage.json을 매 반복 재판독해 합계로 판정한다
    #     (매직 카운트 금지). 무한 루프 방지로 40회 상한을 두고, 초과 시 check()가 FAIL 처리한다.
    MAX_ROUNDS = 40
    rounds = 0
    # UI 대신 page.request.post로 직접 호출한다 — 반복 40회를 매번 폼 채우기·클릭·렌더 대기로
    # 하면 느리고 셀렉터 타이밍에 걸린 플래키 표면만 늘어난다. 게이트 통과 여부 자체는 이후
    # usage_total() 재판독으로 검증하므로 UI 경유가 필요 없다 (게이트 이후 채팅 1회는 실제
    # UI를 통해 별도로 검증한다).
    while usage_total() < DAILY_LIMIT and rounds < MAX_ROUNDS:
        rounds += 1
        resp = page.request.post(
            f"{BASE}/api/chat",
            data=json.dumps({"question": f"프로젝트A 반복 질의 {rounds}", "history": []}),
            headers={"Content-Type": "application/json"},
        )
        resp.text()  # SSE 스트림을 끝까지 소비 — 서버 제너레이터가 완주해야 embed/answer가 기록된다
    check("채팅 반복으로 일일 상한 도달(40회 이내)", usage_total() >= DAILY_LIMIT,
          f"total={usage_total()} rounds={rounds}")

    page.click("nav >> text=소스")
    page.wait_for_selector("#srcTable tbody tr")
    notes_row = page.locator("#srcTable tbody tr", has_text="notes").first
    notes_before = notes_row.locator("td").nth(1).inner_text()
    notes_row.locator("button", has_text="동기화").click()
    page.wait_for_timeout(700)
    notes_row = page.locator("#srcTable tbody tr", has_text="notes").first
    notes_after = notes_row.locator("td").nth(1).inner_text()
    check("상한 도달 후 notes 동기화 스킵(문서 수 불변)", notes_after == notes_before,
          f"before={notes_before} after={notes_after}")

    gated = [e for e in page.request.get(f"{BASE}/api/log").json()
             if e.get("source") == "notes" and e.get("ok") is False]
    check("run_sync 게이트 로그 기록", bool(gated) and "일일 API 호출 상한" in (gated[0].get("error") or ""),
          gated[0]["error"][:60] if gated else "게이트 로그 항목 없음")

    page.click("nav >> text=로그")
    page.wait_for_timeout(300)
    check("로그 탭에 일일 상한 안내 노출", "일일 API 호출 상한" in page.locator("#logBody").inner_text())

    # 참고: index.html의 syncNow()는 /api/sync 응답의 ok/error를 확인하지 않고 loadSources()만
    # 호출한다 — alert(r.error) 경로가 연결돼 있지 않아 상한 게이트를 다이얼로그로 알리지 않는다
    # (2026-08-29 실측 확인 — page.on("dialog")로 관찰 시 게이트 진입 후에도 dialogs가 비어 있음).
    # 백엔드 게이트·로그 노출은 위 두 체크로 충분히 검증했다고 보고 다이얼로그 검증은 생략한다 —
    # 프런트 수정(syncNow에 ok/error 확인 추가)은 이 태스크 범위 밖. docs/HANDOFF.md 참조.

    page.click("nav >> text=채팅")
    page.fill("#question", "프로젝트A 킥오프 다시 알려줘")
    before_cards = page.locator(".src").count()
    page.click("form >> text=검색")
    page.wait_for_function(
        f"document.querySelectorAll('.src').length > {before_cards}", timeout=10000
    )
    check("상한 도달 후에도 채팅 정상 응답(검색·답변 유지)",
          "프로젝트A" in page.locator("#messages").inner_text())

    browser.close()

print("\n".join(results))
print(f"\n총 {len(results)}건 전부 PASS")
