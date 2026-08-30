# llmsearch 작업 인수인계 지시서

> 이 repo를 clone한 뒤 다른 Claude 세션에서 작업을 이어가기 위한 문서.
> 마지막 갱신: 2026-08-31 (Claude 스킬화 머지 완료 — 다음: M9 로컬 임베딩 스파이크)

## 1. 현재 상태

| 마일스톤 | 상태 | 내용 |
|---|---|---|
| M1 코어 | ✅ 머지 | 인덱스(FTS5+vec)·검색(RRF)·채팅 GUI·notes/local_docs 커넥터·골든 평가 |
| M2 Outlook | ✅ 머지 | outlook_mail/outlook_cal, ComWorker STA, 롤링 윈도우 커서, 인용 절단 |
| M3 Confluence/Jira | ✅ 머지 | 인증 3단 폴백(PAT→Basic→쿠키), 페이지 트리 미러, 이슈+댓글, URL 등록 GUI |
| M4 잔여 P1 | ✅ 머지 | PPT 비전 보완(SlideRenderer), 서비스별 자격증명(CONFLUENCE_*/JIRA_*), Archive 워크플로 |
| M5 비용 통제 P2 | ✅ 머지 | UsageTracker(원자적 쓰기·형태 검증), 카운팅 래퍼, run_sync 게이트, E2E 확장 (45/45), 이연 Minor 정리 후속 반영 |
| M6a 운영 완성(설정·재요약·사용량) | ✅ 머지 | rules.md 설정 탭·요약 규칙 주입·notes 인덱싱, 재요약(센티널), 사용량 표시, 로컬 오리진 검사 |
| M6b rebuild | ✅ 머지 | 제자리 초기화·요약 md 재사용·마커 재개·스키마 불일치 배너/복구·CLI --rebuild |
| M7 검색 품질·평가 | ✅ 머지 | 채팅 필터(선검색 강제·툴 기본값·Claude 고지), 툴 스키마 현행화, 출처 발췌, 골든 평가 GUI |
| M8 채팅 UX | ✅ 머지 | 세션 저장/복원(chats.db)·내보내기(export_to_notes)·출처 미리보기 |
| WSL 개발·테스트 지원 정리 | ✅ 머지 | Windows 전용 Outlook 소스(`IS_WINDOWS`/`_outlook_available`) 스케줄러 자동 제외·수동 동기화 안내 메시지·소스 탭 "Windows 전용" 표시, 환경별 README |
| Claude 스킬화 | ✅ 머지 | 전역 설정(`~/.llmsearch`, resolver·load_env), `llmsearch` CLI(search/get/status/sync — GUI 함수 재사용, FTS 폴백, 서버 감지), `skills/llmsearch`(SKILL.md·래퍼·install.sh) — 스펙 `2026-08-31-claude-skill-design.md` |

- 테스트 기준: **438 passed** (`./.venv/bin/pytest`)
- E2E: **80/80** (`tools/e2e/verify.py` — 9.11단계(M7 골든 평가 GUI) `골든 미스 표시` 체크와 10단계 사이에 M8 세션 자동 생성·복원(새로고침)·미리보기·내보내기 시나리오 7건 추가; WSL 지원 변경은 Fake 주입 경로만 건드려 E2E 시나리오 수 불변; Claude 스킬화(문서·CLI·install.sh만 변경, GUI 무변경)는 §2 절차대로 재실행해 80/80 재확인함)
- SDD 진행 원장(.superpowers/)과 워크트리는 **gitignore라 clone에 없다** — 상태는 이 문서가 기준

## 2. 환경 셋업 (clone 직후)

```bash
python3 -m venv .venv
./.venv/bin/pip install -e . pytest
./.venv/bin/pytest          # 438 passed 확인 (WSL 가능, API 키 불필요)
cp config.example.yaml config.yaml   # gitignore 대상 — 로컬 경로 채우기
cp .env.example .env                 # API 키·자격증명 (gitignore 대상)
```

Playwright E2E (선택, sudo 불필요):
```bash
./.venv/bin/pip install playwright
./.venv/bin/playwright install chromium --only-shell
# WSL에서 라이브러리 부족 시: apt-get download libnspr4 libnss3 libasound2t64
#   → dpkg -x 로 임시 폴더에 풀고 LD_LIBRARY_PATH=<폴더>/usr/lib/x86_64-linux-gnu 주입
./.venv/bin/python tools/e2e/demo_server.py &   # Fake 프로바이더 데모 서버 (API 키 불필요)
./.venv/bin/python tools/e2e/verify.py          # 전체 시나리오 검증
```

## 3. 다음 작업: M9 스펙 작성 (로컬 임베딩 스파이크)

M1~M7 머지 완료, M8은 브랜치 완료(머지 대기 — §6 M8 수동 체크리스트 확인 후 머지). 그 외 잔여는 §6 수동 게이트(Windows/사내망)와 §7 파킹된 결정뿐이다. 다음은 M9(확장 — 로컬 임베딩 bge-m3) 스펙 작성 — **실노트북 스파이크 먼저**(CPU 속도·메모리), 성립 시 `LocalEmbeddings` + `embed_provider` 설정, 차원 변경으로 schema 버전 상승. `docs/superpowers/specs/2026-08-29-llmsearch-roadmap-m6-m9.md` 로드맵 참조. 새 마일스톤을 시작하면 아래 표준 절차를 그대로 따른다 (사용자가 매 마일스톤 이 방식을 선택해 왔음):

1. `EnterWorktree`(또는 `git worktree add`)로 격리 브랜치 생성 → **`git merge master`로 최신화 확인** (워크트리가 낡은 HEAD에서 분기된 사례 2회 있었음) → venv 셋업 + 베이스라인 테스트
2. superpowers 스크립트로 태스크별 브리프 추출(`task-brief PLAN N`) → 구현자 서브에이전트 디스패치 (플랜에 전체 코드가 있으므로 저비용 모델로 충분, 통합 태스크는 중간 모델)
3. 태스크마다 `review-package PLAN BASE HEAD`로 diff 생성 → **적대적/전문가/시니어 3관점 리뷰어** 디스패치 → Critical/Important는 fix round(구현자 재개→scoped 재리뷰, 최대 5회) → Minor는 원장에 기록 후 이연
4. 전 태스크 완료 후 **최종 브랜치 리뷰**(최고 성능 모델, 이연 Minor 삼사 포함) → 픽스 웨이브 1회 → scoped 재리뷰
5. Playwright E2E 재검증 (tools/e2e/ 스크립트를 새 기능에 맞게 확장 — 사용자의 상시 요청)
6. finishing-a-development-branch: 전체 스위트 green 확인 → **머지 방식은 메뉴로 사용자에게 질문** (지금까지 항상 "1. master 로컬 머지" 선택 — 머지 결과에서 테스트 재검증 후 워크트리/브랜치 정리)

superpowers 플러그인이 없는 환경이면: 플랜을 태스크 순서대로 직접 구현(TDD 스텝 그대로)하되, 태스크마다 3관점 리뷰를 서브에이전트로 수행하는 원칙은 유지한다.

## 4. 상시 규칙 (사용자 지시 — 생략 금지)

- **모든 산출물에 적대적/전문가/시니어 3관점 리뷰 + 발견 사항 반영.** 스펙·계획·코드 태스크·최종 브랜치 전부. 계획의 참조 코드도 리뷰 대상 — M1~M5 모두 계획 코드에서 Critical급 결함이 리뷰로 잡혔다 (예: vec0 REPLACE 크래시, ComWorker TOCTOU 행, `..` 경로 탈출, TestClient base_url 누락, UsageTracker 락 부재). **계획 문면과 스펙 의도가 충돌하면 스펙 의도가 우선**하되, 충돌 발견 시 사용자에게 확인.
- 웹앱 동작은 Playwright 브라우저 E2E로 검증 (사용자 상시 요청).
- 보안: 자격증명·API 키 `.env` 전용, repr/로그/예외 평문 금지, config.yaml·golden.yaml gitignore 유지, 127.0.0.1 고정.
- 특성화 테스트: 기대값 변경은 의도된 계약 변경일 때만, 리뷰어에게 명시.

## 5. 문서 지도

- 스펙(승인본): `docs/superpowers/specs/2026-08-17-llmsearch-design.md` — 결정 기록·M3/M4 구현 노트 포함
- 계획: `docs/superpowers/plans/2026-08-17-llmsearch-m1.md` ~ `2026-08-18-llmsearch-m5.md` (각 계획 말미에 수동 체크리스트)
- M6 스펙: `docs/superpowers/specs/2026-08-29-llmsearch-m6-design.md`, 로드맵: `docs/superpowers/specs/2026-08-29-llmsearch-roadmap-m6-m9.md`, 계획: `docs/superpowers/plans/2026-08-29-llmsearch-m6a.md`(말미에 M6a 수동 체크리스트), `docs/superpowers/plans/2026-08-29-llmsearch-m6b.md`(말미에 M6b 수동 체크리스트)
- M7 스펙: `docs/superpowers/specs/2026-08-29-llmsearch-m7-design.md`, 계획: `docs/superpowers/plans/2026-08-29-llmsearch-m7.md`(말미에 M7 수동 체크리스트)
- M8 스펙: `docs/superpowers/specs/2026-08-29-llmsearch-m8-design.md`, 계획: `docs/superpowers/plans/2026-08-29-llmsearch-m8.md`(말미에 M8 수동 체크리스트)
- 프로젝트 규칙: `CLAUDE.md` (repo 루트 — 세션 자동 로드)
- E2E: `tools/e2e/` (Fake 데모 서버 + 검증 스크립트)

## 6. 수동 게이트 (Windows / 사내망 — 사용자 실행 대기 중)

1. `python scripts/check_outlook.py` — Outlook COM·Restrict 로케일(월/일 순서)·EX 발신자 SMTP 확인 (M2)
2. `python scripts/check_ppt_render.py <pptx>` — PowerPoint COM 렌더링. 부분 손상·암호 pptx로 모달 없이 실패하는지도 확인 (M4; POWERPNT.EXE 상주는 의도된 동작)
3. M3 사내망 체크리스트 — 실 Confluence/Jira base URL 설정, 인증 진단, 등록·동기화·출처 열기 (m3 계획 말미 5항목)
4. M4 체크리스트 — 실 pptx 비전 요약(Gemini 유료 키), 서비스별 자격증명, GUI 아카이브 (m4 계획 말미)
5. M5 체크리스트 — `limits.daily_api_calls: 5` 같은 작은 값으로 동기화 반복 → 로그 탭 "일일 API 호출 상한" 안내 + 채팅 유지 확인, `data_dir/usage.json` 일자별 누적 확인 후 0으로 복원 (m5 계획 말미)
6. M6a 체크리스트 — 설정 탭 `## 답변 규칙` 변경 후 재시작 없이 채팅 문체 반영, `## 요약 규칙`에 "수치는 표로" 추가 후 재요약 시 요약 md에 표 생성(실 Gemini), 소스 탭 사용량 줄 동기화/채팅마다 갱신 및 `limits.daily_api_calls` 반영 확인 (m6a 계획 말미)
7. M6b: 실 데이터로 [인덱스 재구축] 1회 — summary/vision 카운트 불변 확인, `--rebuild --yes` 헤드리스 확인
8. M7: 실 Claude로 필터 질의 시 고지가 답변에 반영되는지, sender 필터로 메일만 나오는지, golden.yaml 실데이터 실행
9. M8: 실 Claude 세션에서 후속 질문이 이전 맥락을 잇는지, 새로고침 복원, 내보내기 md 확인

## 7. 파킹된 결정 (구현 전 사용자 확인 필요)

- **keyring 저장 도입**: 스펙 §7.2/§10은 keyring 언급, 현재는 `.env` 대체로 연기 (스펙 M3 구현 노트). 개인용 로컬 툴로는 .env로 충분하다는 판단 — 도입하려면 사용자와 범위 합의.
- ComWorker submit 타임아웃: COM 모달 행 대비책이지만 STA 스레드가 어차피 wedged로 남아 실익 제한 — 보류.
- 회사 일정 시스템 연동: 스펙 Out of Scope (커넥터 인터페이스로 확장 가능하게만 설계됨).
- **local_docs 정상 동기화의 미존재 폴더 삭제 판정**: 감시 폴더(네트워크 드라이브 등)가 한 라운드 미마운트면 그 아래 문서가 전부 deleted로 판정되어 요약 md·복사본이 unlink된다 (M6b 최종 리뷰 지적, 선재 동작). CLAUDE.md "보수적 삭제"와 충돌 — 미존재 폴더의 prev sid를 `RETRY_SENTINEL`로 이월하는 소규모 픽스 후보 (M7 전 처리 권장). rebuild 경로는 precheck·force_reindex로 이미 보호됨.
- 이연 Minor 잔여분은 각 마일스톤 최종 리뷰에서 전부 OK-TO-DEFER 판정 — 필요 시 git log의 리뷰 반영 커밋들 참조.

## 8. 함정·이력 (같은 실수 반복 금지)

- 워크트리 분기점이 낡을 수 있음 — 생성 직후 `git merge master`.
- vec0는 INSERT OR REPLACE 미지원 → DELETE+INSERT 패턴 유지 (db.py).
- Gemini 임베딩 768 MRL은 **L2 재정규화 필수** (embeddings.py `_l2_normalize`).
- Outlook Restrict는 분 단위 절단 → `_RESTRICT_SLACK` 확장 + Python 정확 후필터 병행 (outlook 커넥터).
- 일시 장애 vs 삭제 구분: `_RETRY_SENTINEL`(local_docs), 미스 카운터+safe-round(confluence/jira) — 이 패턴을 깨면 데이터 소실.
- 웹 테스트의 TestClient는 `base_url="http://127.0.0.1"` 필수 (TrustedHostMiddleware).
- chunking.py/embeddings.py 2개 파일만 레거시 탭 들여쓰기 — 나머지는 4칸 공백.
- WSL은 개발·테스트 전용 — Outlook 소스는 자동 제외(`_outlook_available`), 데모/테스트는 Fake 주입으로 동작.
