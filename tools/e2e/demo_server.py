"""Playwright E2E용 데모 서버 — 전 소스 Fake 프로바이더 (API 키·사내망·Windows 불필요).

사용: ./.venv/bin/python tools/e2e/demo_server.py   (127.0.0.1:8642)
데이터: <repo>/.e2e-data/ (gitignore, 기동 시 초기화)
daily_api_call_limit=50 — 기존 시나리오(동기화 6종+채팅 1회)는 도달하지 않고, 채팅 반복으로는 도달 가능한 값.
"""
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn

from llmsearch.atlassian.client import FakeAtlassianClient
from llmsearch.config import Config
from llmsearch.connectors import local_docs
from llmsearch.embeddings import FakeEmbeddings
from llmsearch.llm import FakeAnswerer
from llmsearch.outlook.client import FakeOutlookClient
from llmsearch.render import FakeSlideRenderer
from llmsearch.summarize import FakeSummarizer
from llmsearch.web.app import create_app

BASE = Path(__file__).resolve().parents[2] / ".e2e-data"
shutil.rmtree(BASE, ignore_errors=True)

NOTES = BASE / "notes"
NOTES.mkdir(parents=True)
(NOTES / "kickoff.md").write_text(
    "# 프로젝트A 킥오프\n8월 1일 킥오프 진행. 담당자 김철수. 일정과 범위 확정.", encoding="utf-8"
)
(NOTES / "retro.md").write_text("# 프로젝트A 회고\n8월 15일 회고. 개선점: 리뷰 주기 단축.",
                                encoding="utf-8")

WATCH = BASE / "watch"
WATCH.mkdir(parents=True)
(WATCH / "프로젝트A_아키텍처.pptx").write_bytes(b"fake pptx bytes")
# 실 markitdown 대신 짧은 텍스트 스텁 — 비전 증강 경로를 태운다
local_docs.extract_text = lambda p: "프로젝트A 아키텍처 표지"

NOW = datetime.now()
outlook = FakeOutlookClient(
    mails={"inbox": [{
        "entry_id": "m1", "subject": "프로젝트A 결정사항 공유",
        "body": "회의 결과: 출시일 9월 1일로 확정.",
        "sender_name": "김철수", "sender_email": "kim@corp.com",
        "received_at": NOW - timedelta(days=1), "folder": "inbox",
    }]},
    appointments=[{
        "entry_id": "e1", "subject": "주간 팀 미팅", "body": "안건: 진행 상황 점검",
        "location": "회의실A", "start": NOW + timedelta(days=2),
        "end": NOW + timedelta(days=2, hours=1), "attendees": "나; 김철수",
    }],
)


def page(pid, title, ancestors=None, html="<p>프로젝트A 위키 본문</p>"):
    return {"id": pid, "space": "ENG", "title": title, "html": html, "version": 1,
            "updated": "2026-08-10T10:00:00", "ancestors": ancestors or [],
            "url": f"https://wiki.example.com/pages/{pid}"}


atlassian = FakeAtlassianClient(
    pages={"100": page("100", "프로젝트A 홈"),
           "101": page("101", "설계 문서", ancestors=["프로젝트A 홈"],
                        html="<h2>설계</h2><p>하이브리드 검색 구조</p>")},
    children={"100": ["101"]},
    issues={"PROJ-1": {"key": "PROJ-1", "summary": "검색 버그 수정", "description": "재현 절차",
                        "status": "Open", "assignee": "김철수",
                        "updated": "2026-08-12T09:00:00",
                        "url": "https://jira.example.com/browse/PROJ-1",
                        "comments": [{"author": "박영희", "created": "2026-08-12T10:00:00",
                                       "body": "원인 확인했습니다"}]}},
)

renderer = FakeSlideRenderer(images={"프로젝트A_아키텍처.pptx": [b"png1", b"png2"]})

cfg = Config(data_dir=BASE / "data", notes_folders=[NOTES], watch_folders=[WATCH],
             projects=["프로젝트A"], daily_api_call_limit=50)
app = create_app(cfg, embedder=FakeEmbeddings(), summarizer=FakeSummarizer(),
                 answerer=FakeAnswerer(), outlook_client=outlook,
                 atlassian_client=atlassian, slide_renderer=renderer,
                 enable_scheduler=False)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8642, log_level="warning")
