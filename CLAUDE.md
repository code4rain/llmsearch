# llmsearch

개인용 통합 문서 검색 GUI (Korean corporate, Windows 실사용 + WSL2 개발). 5개 소스(로컬 문서 요약, Confluence/Jira 미러, 개인 노트, Outlook 메일/일정)를 인덱싱하고 LLM으로 자연어 질의에 출처와 함께 답한다.

## 🛠 Commands
- Setup: `python3 -m venv .venv && ./.venv/bin/pip install -e . pytest`
- Test: `./.venv/bin/pytest` (WSL 실행 가능, API 키 불필요 — 전부 Fake/Mock)
- Run: `./.venv/bin/python -m llmsearch` (실사용은 Windows Python — COM 필요)
- E2E: `docs/HANDOFF.md`의 Playwright 절 참조
- CLI/스킬: `./.venv/bin/llmsearch {search|get|status|sync}` — 설치 `skills/llmsearch/scripts/install.sh` (전역 설정 `~/.llmsearch/`)

## 📐 Architecture & Standards
- Python 3.12 · FastAPI(127.0.0.1 고정) · SQLite(FTS5+sqlite-vec/numpy 폴백) · 요약=Gemini, 답변=Claude(`claude-opus-5`), 임베딩=Gemini 768차원 MRL+L2 재정규화
- 외부 연동은 전부 **Protocol + Fake 주입** (OutlookClient/AtlassianClient/SlideRenderer/Summarizer/EmbeddingProvider) — 실구현은 지연 import, 테스트는 Fake만. COM은 `ComWorker` STA 스레드 1개 공유
- 삭제 판정은 보수적: 미스 카운터(연속 3회) + safe-round sweep — 일시 장애를 삭제로 오판하지 않는다. `run_sync`는 커넥터 state 전체(misses 포함)를 가감 없이 왕복 저장할 것
- **자격증명·API 키는 `.env`에서만** — config.yaml·코드·로그·예외 메시지·repr 평문 금지 (`repr=False`). `config.yaml`/`golden.yaml`은 gitignore(회사 데이터)
- 웹 보안: TrustedHostMiddleware, `/api/open`은 인덱스 등록 경로/URL만, UI 동적 값은 `esc()` 이스케이프. 원격 문자열(페이지 제목 등)을 경로에 쓸 때는 `_sanitize_segment` + `relative_to` 2계층 검증
- 리팩터·수정 전 특성화 테스트로 현재 동작 고정. 테스트 실패 시 구현이 아니라 **기대값**을 실동작에 맞춰 고친다 (의도 변경일 때만 기대값 변경을 명시)
- **모든 산출물(스펙·계획·코드·최종 브랜치)에 적대적/전문가/시니어 3관점 리뷰를 수행하고 발견 사항을 반영한다** — 이 프로젝트의 상시 지시. 계획의 참조 코드도 리뷰 대상 (계획 코드 결함이 반복된 이력 있음 — 계획 문면보다 스펙 의도가 우선)
- Python 들여쓰기 4칸 (chunking.py/embeddings.py 2개만 레거시 탭 — 그 파일 수정 시만 탭 유지)
- 스펙 `docs/superpowers/specs/`, 계획 `docs/superpowers/plans/`, **작업 인수인계는 `docs/HANDOFF.md`**
