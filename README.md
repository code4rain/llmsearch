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

## 개발
- 테스트: `pytest` (WSL에서 실행 가능, API 키 불필요)
- 인덱스는 소모품: 스키마 변경·손상 시 `index.db` 삭제 후 재동기화
