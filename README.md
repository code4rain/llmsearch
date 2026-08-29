# llmsearch

개인용 통합 문서 검색 툴. 스펙: `docs/superpowers/specs/2026-08-17-llmsearch-design.md`

## 설치 (Windows Python 기준)
1. `pip install -e ".[vec]"` (sqlite-vec 실패 시 `pip install -e .` — numpy 폴백 자동)
2. `python scripts/spike_sqlite_vec.py` 로 벡터 확장 동작 확인
3. `config.example.yaml` → `config.yaml` 복사 후 경로 수정
4. `.env.example` → `.env` 복사 후 API 키 기입 (Gemini는 **유료 티어 필수** — 무료 티어는 입력이 학습에 사용됨)

## 실행
`python -m llmsearch --config config.yaml` → http://127.0.0.1:8642

## 평가
`golden.yaml`에 `[{question, expect_source_id}]` 작성 후:
`python -m llmsearch.eval.golden --config config.yaml --golden golden.yaml`

## Outlook 연동 (M2, Windows 전용)
1. Windows Python에 `pip install -e ".[vec,win]"` (pywin32 포함)
2. Outlook 데스크톱 앱을 실행해 둔 상태에서 `python scripts/check_outlook.py`로 연동 점검
3. 앱 실행 후 소스 탭에서 outlook_mail / outlook_cal 동기화 — 초기 메일 인덱싱은
   배치(기본 200통)로 진행되며 중단해도 다음 동기화가 이어서 처리한다 (backlog 표시)
- WSL/테스트 환경에서는 Outlook 소스 동기화 시 안내 오류가 로그에 남고 다른 소스는 정상 동작

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

## 비용 통제

- API 호출(임베딩 embed / 요약 summary / 비전 vision / 답변 answer)은 `data_dir/usage.json`에
  일자별로 집계되고 호출 때마다 로그로 남는다 (최근 30일 보관). 카운트는 논리 호출 단위라
  실제 API 호출보다 적게 셀 수 있다 — 임베딩 1건은 내부적으로 100건 배치 여러 번일 수 있고,
  답변 1건은 도구 라운드에 따라 스트림 호출 최대 4회다.
- `config.yaml`의 `limits.daily_api_calls`로 일일 상한을 걸 수 있다 (기본 0 = 무제한).
  상한 도달 시 **요약·인덱싱(동기화)만 일시정지**되고 검색·채팅 답변은 계속 동작한다 —
  동기화 로그에 안내가 남고, 다음 날 자동 재개된다.

## 개발
- 테스트: `pytest` (WSL에서 실행 가능, API 키 불필요)
- 인덱스는 소모품: 스키마 변경·손상 시 `index.db` 삭제 후 재동기화
