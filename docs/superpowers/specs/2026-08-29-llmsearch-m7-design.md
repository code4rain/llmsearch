# llmsearch M7 — 검색 품질·평가 설계

> 상위 스펙: `2026-08-17-llmsearch-design.md` §1(성공 기준), §8(검색 툴·답변 플로우), §12(골든 평가). 로드맵: `2026-08-29-llmsearch-roadmap-m6-m9.md`.
> 목표: 사용자가 검색 범위를 직접 좁히고(필터), 답변 근거를 카드에서 바로 확인하며(발췌), 골든 세트로 검색 품질을 GUI에서 측정한다. 새 외부 의존성 없음.

## 1. 범위

| 기능 | 근거 | 산출물 |
|---|---|---|
| 채팅 필터 | §8 "구조화 필터 (P0)" — 현재는 Claude 툴만 필터를 쓸 수 있고 사용자는 못 씀 | 채팅 폼에 소스·기간·발신자 필터, `/api/chat` `filters`, 선검색 강제·툴 검색 기본값 |
| 검색 툴 스키마 현행화 | §8 검색 툴 시그니처에 `sender`가 있으나 Claude 툴 스키마에 없고, 소스 enum이 M1 시점(notes/local_docs)에 멈춤 | `_SEARCH_TOOL`에 6개 소스·`sender` 추가 |
| 출처 카드 발췌 | §8 답변 응답 "출처 카드" — 어떤 청크가 매칭됐는지 사용자가 볼 수 없음 | `Hit.snippet`(최고 점수 청크 앞부분), 카드에 표시 |
| 골든 평가 GUI | §1 성공 기준·§12 — 현재 CLI 전용(`python -m llmsearch.eval.golden`), 실 API 키·카운팅 우회 | 설정 탭 평가 절: `golden.yaml` 편집·실행·질문별 순위 표, `GET/PUT /api/eval/golden`, `POST /api/eval/golden/run` |

범위 밖: 검색 파라미터(RRF_K·CANDIDATES 등) GUI 튜닝, 평가 이력 저장, 재랭킹 모델, 필터 프리셋 저장, 답변 품질(LLM-judge) 평가.

## 2. 채팅 필터

**UI** — 채팅 폼 아래 접이식 "필터" 행: 소스 체크박스 6개(기본 전부 해제 = 제한 없음), 기간 `date_from`/`date_to`(`<input type="date">`), 발신자 텍스트(메일 전용, 이메일 주소). 값은 `filters` 객체로 `/api/chat` 페이로드에 포함: `{"source_filter": [...]|null, "date_from": "YYYY-MM-DD"|null, "date_to": ..., "sender": ...|null}`. 빈 값은 null. 질문 div 아래에 적용된 필터를 한 줄 표시(`textContent`).

**서버** — `/api/chat`은 `payload.get("filters") or {}`를 검증(소스는 `SOURCES` 안의 문자열만, 날짜는 `YYYY-MM-DD` 정규식, sender는 문자열 ≤ 200자; 위반 400)하고 `search_fn` 래퍼로 적용한다:
- **선검색(강제)**: `answer_stream`이 `search_fn(question)`으로 호출 → 키워드 인자가 전부 None이므로 필터 값이 그대로 들어간다.
- **툴 검색(기본값)**: Claude가 `search_fn(query, source_filter=..., ...)`로 호출 → **None인 인자만 필터로 채운다**(Claude가 명시한 값이 우선). 사용자가 소스를 좁혀도 Claude가 근거 부족을 판단해 다른 소스를 명시적으로 뒤질 수 있게 — "강제"는 첫 검색에만.
- 구현: `_apply_filters(search_fn, filters)`를 `web/app.py` 모듈 함수로 두고 단위 테스트한다. `llm.py`는 무변경.

**검색 툴 스키마** — `_SEARCH_TOOL.input_schema.properties.source_filter.items.enum`을 `["notes","local_docs","outlook_mail","outlook_cal","confluence","jira"]`로, `sender: {"type":"string","description":"보낸 사람 이메일(메일 전용, 선택)"}` 추가, description에 "메일·일정·Confluence·Jira 포함" 반영. `answer_stream`의 툴 호출은 이미 `sender`를 넘길 수 있게 `args.get("sender")` 추가(현재 누락).

## 3. 출처 카드 발췌

- `models.Hit`에 `snippet: str = ""` 추가(끝에 기본값 — 기존 위치 인자 생성자 호환).
- `search.search`: 문서별 최고 RRF 청크(`doc_best_chunk`)의 텍스트에서 청크 헤더 `[제목 | 날짜] `를 제거한 앞 `SNIPPET_CAP = 200`자. 승격 본문(`excerpt`)과 별개 — LLM 컨텍스트(`_hits_block`)는 무변경.
- UI 카드: 제목 줄 아래 `<div class="snip">`에 `esc(h.snippet)` (빈 값이면 생략). CSS: 회색 작은 글씨, 2줄 말줄임.
- `/api/chat` SSE `sources` 이벤트는 `asdict(h)`라 자동 포함.

## 4. 골든 평가 GUI

**파일** — `data_dir/golden.yaml` (gitignore 유지). 형식은 기존 CLI와 동일: `[{question, expect_source_id}]`. `expect_source_id`는 정확 일치 또는 경로 접미사(`_matches`).

**API**
- `GET /api/eval/golden` → `{"text": str, "path": str, "count": n}` — 파일 없으면 템플릿 주석 2줄 + 예시 1건(주석 처리). `count`는 파싱 결과 건수(파싱 실패 0).
- `PUT /api/eval/golden {"text"}` → YAML 파싱 검증(리스트이고 각 항목에 두 키 문자열, 위반 400 + 사유), 원자적 저장, `{"ok": true, "count": n}`. 로컬 오리진 검사 적용. 크기 상한 256KB.
- `POST /api/eval/golden/run` (로컬 오리진) → `_require_db`; 파일 없거나 0건이면 400; `golden.evaluate(state["read_conn"], state["embedder"], cases)` 실행 — **카운팅 임베더를 거치므로 usage에 embed로 기록**된다(질의 캐시 적중 시 미기록). 검색 경로이므로 일일 상한 게이트와 무관(상위 스펙 §10). 응답 `{"total", "hit_at_3", "rate", "cases": [{"question", "expected", "rank": 1|2|3|null, "got": [source_id×3]}]}`, `"target": 0.7`, `"pass": rate >= 0.7`.
- `evaluate` 확장: 기존 반환에 `cases`(질문별 순위) 추가 — CLI 출력 호환(기존 키 유지). `rank`는 `got` 안에서 `_matches`가 처음 참인 인덱스+1.

**UI** — 설정 탭에 "검색 품질 평가" 절: 경로, textarea(`.value`), [저장] [평가 실행(N건)] 버튼, 결과: 헤더 줄 "상위3 적중률 50% (1/2) — 목표 70% ❌/✅" + 표(질문 / 기대 / 순위 / 상위 3 결과). 실행 전 confirm("N건 질의 임베딩 API 호출"). 결과 표는 `textContent`/`esc()`.

**CLI** — `python -m llmsearch.eval.golden`은 유지하되 `--golden` 기본값을 `data_dir/golden.yaml`로(생략 가능). 카운팅 우회 주석은 유지(실 API 직접 호출).

## 5. 오류 처리

| 상황 | 동작 |
|---|---|
| 필터 값 비정상(미지 소스·날짜 형식·sender 과대) | 400, 검색 미실행 |
| golden.yaml 파싱 실패(PUT) | 400 + YAML 오류 위치, 파일 미변경 |
| golden.yaml 없음/0건(run) | 400 "golden.yaml에 질문을 먼저 작성하세요" |
| 평가 중 임베딩 API 실패 | 500 대신 `{"error": ...}` 200? — 아니오: `evaluate`는 예외를 전파하고 엔드포인트가 502 `{"detail": "임베딩 호출 실패: ..."}`(자격증명·키는 메시지에 포함 금지 — 예외 문자열 그대로 노출하지 않고 클래스명만) |
| 스키마 불일치 상태 | `_require_db` 503 |

## 6. 테스트

- 단위: `_apply_filters`(선검색 강제·툴 호출 우선·None 채움), `/api/chat` 필터 검증 400·정상 적용(FakeAnswerer가 `search_fn(question)`만 호출 → notes 필터 시 local_docs 문서 미포함), `_SEARCH_TOOL` enum·sender, `answer_stream` 툴 호출이 `sender`를 전달(Fake 클라이언트로 tool_use 시뮬레이션 — 기존 test_llm 패턴 재사용), `Hit.snippet` 생성(헤더 제거·200자·최고 청크 기준)·기존 `Hit` 위치 인자 호환, golden GET 템플릿/PUT 검증·원자성/run 결과(rank·pass)·0건 400·오리진 403, `evaluate` `cases` 확장 + CLI 출력 키 호환, `--golden` 기본값.
- E2E(9.9 뒤, `# 10.` 앞; 기존 66건 무변경): 필터 행에서 notes만 선택 → 질문 → 출처 카드 전부 notes + 적용 필터 표시; 카드에 발췌(`.snip`) 존재; 설정 탭 평가 절에 golden 2건 저장 → 실행 → 결과 헤더에 "1/2" 또는 "2/2" 및 표 2행. 예산: 채팅 2 + 평가 임베딩 2 ≈ +4 (≈ 34 < 50).

## 7. 결정 기록

| 결정 | 근거 |
|---|---|
| 필터는 선검색 강제·툴 검색 기본값(None만 채움) | 사용자 범위 제한과 Claude의 다중 홉 탐색 자유를 동시에 보존 — llm.py 무변경 |
| 발췌는 최고 청크 200자, LLM 컨텍스트 무변경 | 사용자 확인용 표시이지 답변 입력이 아님 — 토큰·품질 영향 0 |
| 골든 평가를 GUI에서 카운팅 임베더로 실행 | 실키 CLI 우회 대신 앱 예산 안에서 측정; 검색 경로라 상한 게이트 대상 아님 |
| golden.yaml은 설정 탭에서 편집 | rules.md와 같은 패턴(원자적 저장·256KB) — 도구 일관성 |
| 평가 결과는 저장하지 않음 | 회귀 비교는 사용자가 수동(범위 밖 — M8 이후 후보) |
