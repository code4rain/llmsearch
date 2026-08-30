# llmsearch

개인용 통합 문서 검색 툴. 스펙: `docs/superpowers/specs/2026-08-17-llmsearch-design.md`

## 실행 환경

- **Windows(실사용)**: `py -3.12 -m venv .venv && .venv\Scripts\pip install -e ".[win,vec]"` →
  `python -m llmsearch --config config.yaml`. Outlook 동기화·PowerPoint COM 비전 보완·파일
  열기(`/api/open`)가 전부 동작한다.
- **WSL(개발·테스트)**: `python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"`
  (sqlite-vec 미설치 시 numpy 폴백 자동) → `./.venv/bin/pytest`, 데모 서버·E2E는
  `tools/e2e/` 참조. Outlook 소스(outlook_mail/outlook_cal)는 소스 탭에 "Windows 전용"으로
  표시되고 스케줄러 대상에서 자동 제외되며, 수동 동기화도 트레이스백 없이 안내 메시지만
  반환한다 — 테스트·데모 서버가 `FakeOutlookClient`를 주입하는 경우에는 WSL에서도 정상
  동작한다. PowerPoint 비전 보완·파일 열기는 COM 의존이라 비활성. `config.yaml`의 경로는
  `/mnt/d/...` 형식으로 설정한다.

## 설치 (Windows Python 기준)
1. `pip install -e ".[vec]"` (sqlite-vec 실패 시 `pip install -e .` — numpy 폴백 자동)
2. `python scripts/spike_sqlite_vec.py` 로 벡터 확장 동작 확인
3. `config.example.yaml` → `config.yaml` 복사 후 경로 수정
4. `.env.example` → `.env` 복사 후 API 키 기입 (Gemini는 **유료 티어 필수** — 무료 티어는 입력이 학습에 사용됨)

## 실행
`python -m llmsearch --config config.yaml` → http://127.0.0.1:8642

## Claude 스킬로 쓰기

어느 디렉터리의 Claude Code 세션에서든 인덱스를 검색해 출처와 함께 답하게 한다. 답변은 세션이 쓰고, 검색·문서 조회·상태·동기화는 결정적 CLI가 수행한다.

1. `skills/llmsearch/scripts/install.sh` — `~/.llmsearch/{config.yaml,.env,env}` 초기화(있으면 보존) + `~/.claude/skills/llmsearch` 링크
2. `~/.llmsearch/config.yaml`의 `data_dir` 등을 실제 값으로, `~/.llmsearch/.env`에 `GEMINI_API_KEY`를 기입 (키가 없으면 키워드(FTS) 검색만 수행)
3. `skills/llmsearch/scripts/llmsearch status`로 확인

설정 우선순위: `--config` > `LLMSEARCH_CONFIG` > `~/.llmsearch/config.yaml` (`LLMSEARCH_HOME`로 이동 가능). 서버(`python -m llmsearch`)도 같은 규칙을 쓰므로 `--config`를 생략할 수 있다.

CLI: `llmsearch search "질의" [--source S] [--from D] [--to D] [--sender X] [-k N] [--excerpt] [--json]` · `get SOURCE_TYPE ID [--max-chars N]` · `status` · `sync SOURCE|all [--port N]`(서버 실행 중이면 거부 — GUI가 기본 8642가 아닌 포트면 같은 `--port`를 넘겨야 감지된다). exit: 0 성공 / 1 실패 / 2 설정·인자 / 3 서버 실행 중 / 4 스키마 불일치.

## 평가
`golden.yaml`에 `[{question, expect_source_id}]` 작성 후:
`python -m llmsearch.eval.golden --config config.yaml --golden golden.yaml`
설정 탭에서도 편집·실행 가능

## Outlook 연동 (M2, Windows 전용)
1. Windows Python에 `pip install -e ".[vec,win]"` (pywin32 포함)
2. Outlook 데스크톱 앱을 실행해 둔 상태에서 `python scripts/check_outlook.py`로 연동 점검
3. 앱 실행 후 소스 탭에서 outlook_mail / outlook_cal 동기화 — 초기 메일 인덱싱은
   배치(기본 200통)로 진행되며 중단해도 다음 동기화가 이어서 처리한다 (backlog 표시)
- WSL 등 비-Windows 환경에서는 outlook_mail/outlook_cal이 소스 탭에 "Windows 전용"으로
  표시되고 스케줄러에서 자동 제외된다(수동 동기화는 안내 메시지만 반환, 다른 소스는 정상
  동작) — 상세는 위 "실행 환경" 절 참조

## Confluence / Jira 연동 (M3)
1. `config.yaml`의 `atlassian:` base URL 2종 설정, `.env`에 인증(PAT 권장 — DC 7.9+; 안 되면 사번/비밀번호, 최후엔 브라우저 쿠키)
2. 소스 탭 하단 폼에 Confluence 페이지 URL(하위 트리 포함 수집) 또는 Jira 이슈 URL 등록
3. confluence / jira 동기화 — 미러는 `data_dir/confluence/`, `data_dir/jira/`에 Markdown으로 저장됨
- 인증 진단은 첫 동기화 때 PAT→Basic→쿠키 순서로 자동 시도, 실패 시 로그 탭에 안내
- **자격증명 설정**: 공용 `ATLASSIAN_*` 변수로 두 서버를 함께 인증하거나, 서버별로 다르면
  `CONFLUENCE_*` / `JIRA_*` 프리픽스로 각각 설정한다 (서비스 전용 변수가 공용보다 우선).
  DC의 PAT·세션쿠키는 인스턴스별 발급이므로 병용 시 서비스별 설정 또는 Basic(사내 AD 계정)
  모드를 권장한다.

## 프로젝트 아카이브 (PARA)

소스 탭의 "프로젝트 아카이브"에서 완료된 프로젝트를 `완료 처리`하면
`summaries/Projects/<이름>/` 폴더가 `summaries/Archives/<이름>/`으로 이동하고
인덱스가 함께 갱신된다. Archives 문서는 검색에서 제외되지 않고 순위만 하향된다.
완료 처리 후 `config.yaml`의 `para.projects`에서 해당 프로젝트를 제거할 것 —
활성 목록에 남아 있으면 새 문서가 다시 그 프로젝트로 분류될 수 있다.
완료 처리 후 watch 폴더의 원본 파일을 삭제하면 다음 local_docs 동기화의 삭제 전파로
Archives의 요약본·복사본·인덱스도 함께 제거된다 — 아카이브를 보존하려면 원본을 남겨둘 것.

## 대화 저장·내보내기

- 채팅 탭의 대화는 `data_dir/chats.db`에 자동 저장된다 — 인덱스 재구축과 무관하게 보존된다.
  세션 목록에서 이전 대화를 복원하거나 삭제할 수 있다.
- 대화 화면의 [내보내기]는 `data_dir/exports/chat-<id>-<제목>.md`로 저장한다. 같은 세션은
  항상 파일 1개 — 제목이 바뀌면(예: 기본 제목 → 첫 질문으로 자동 변경) 이전 이름 파일은
  정리되고 새 이름으로 다시 쓰인다. 첫 줄에 `[대화기록]` 표식과 1차 출처가 아니라는 안내가
  붙는다.
- `config.yaml`의 `chat.export_to_notes: true`면 이 md 파일들이 notes 동기화 대상에 포함되어
  검색에도 걸린다 (기본은 false).
- 세션을 삭제해도 이미 내보낸 md 파일은 남는다(필요 시 수동 삭제).

## 비용 통제

- API 호출(임베딩 embed / 요약 summary / 비전 vision / 답변 answer)은 `data_dir/usage.json`에
  일자별로 집계되고 호출 때마다 로그로 남는다 (최근 30일 보관). 카운트는 논리 호출 단위라
  실제 API 호출보다 적게 셀 수 있다 — 임베딩 1건은 내부적으로 100건 배치 여러 번일 수 있고,
  답변 1건은 도구 라운드에 따라 스트림 호출 최대 4회다.
- `config.yaml`의 `limits.daily_api_calls`로 일일 상한을 걸 수 있다 (기본 0 = 무제한).
  상한 도달 시 **요약·인덱싱(동기화)만 일시정지**되고 검색·채팅 답변은 계속 동작한다 —
  동기화 로그에 안내가 남고, 다음 날 자동 재개된다.

## 인덱스 재구축

스키마 변경이나 손상으로 `index.db`를 새로 만들어야 할 때, 설정 탭의 [인덱스 재구축]
버튼 또는 `python -m llmsearch --config config.yaml --rebuild [--yes]`로 실행한다.
기존 요약 md는 재사용되어 변경되지 않은 문서는 요약 API를 다시 호출하지 않지만,
임베딩은 전 문서에 대해 다시 계산된다 — 일일 상한을 넘길 수 있으니 유의할 것.
재수집은 백그라운드로 진행되며 도중에 중단되면 다음 기동 시 배너에 [재개] 버튼이 뜬다.
`index.db` 스키마가 코드와 맞지 않으면 배너에 [재구축] 버튼이 자동으로 뜬다.
스키마 불일치 복구마다 손상된 `index.db`는 `.corrupt-<타임스탬프>`로 백업이 남으므로,
복구가 끝나 정상 동작을 확인한 뒤 필요하면 수동으로 지울 것.

## 개발
- 테스트: `pytest` (WSL에서 실행 가능, API 키 불필요)
- 인덱스는 소모품: 스키마 변경·손상 시 설정 탭 [인덱스 재구축] 또는
  `python -m llmsearch --config config.yaml --rebuild` — `index.db`를 직접 지우지 말 것
  (요약 md 매핑이 유실되어 전량 재요약·중복 md가 생김)
