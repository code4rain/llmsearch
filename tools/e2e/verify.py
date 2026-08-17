"""Playwright 브라우저 E2E — demo_server.py(127.0.0.1:8642) 대상 전체 시나리오.

사용: demo_server.py를 먼저 띄운 뒤 ./.venv/bin/python tools/e2e/verify.py
(WSL에서 chromium 라이브러리 부족 시 docs/HANDOFF.md §2의 LD_LIBRARY_PATH 방법 참조)

시나리오: 소스 6종 → Atlassian URL 등록/dedup → 6종 동기화(비전 증강 pptx 포함) →
아카이브 목록 → 채팅+출처 → 열기 버튼 → 프로젝트 완료 처리(디스크 이동 확인) →
등록 삭제 → 로그.
"""
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

    browser.close()

print("\n".join(results))
print(f"\n총 {len(results)}건 전부 PASS")
