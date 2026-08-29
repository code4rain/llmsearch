# llmsearch M7 — 검색 품질·평가 설계

> 상위 스펙: `2026-08-17-llmsearch-design.md` §1(성공 기준), §8(검색 툴·답변 플로우), §12(골든 평가). 로드맵: `2026-08-29-llmsearch-roadmap-m6-m9.md`.
> 목표: 사용자가 검색 범위를 직접 좁히고(필터), 답변 근거를 카드에서 바로 확인하며(발췌), 골든 세트로 검색 품질을 GUI에서 측정한다. 새 외부 의존성 없음.
> 3관점 리뷰(2026-08-29) 반영본 — Critical 2·Important 9·Minor 14 전부 반영.

## 1. 범위

| 기능 | 근거 | 산출물 |
|---|---|---|
| 채팅 필터 | §8 "구조화 필터 (P0)" — 현재는 Claude 툴만 필터를 쓸 수 있고 사용자는 못 씀 | 채팅 폼에 소스·기간·발신자 필터, `/api/chat` `filters`, 선검색 강제·툴 검색 기본값, Claude에게 필터 고지 |
| 검색 툴 스키마 현행화 | §8 검색 툴 시그니처에 `sender`가 있으나 Claude 툴 스키마에 없고, 소스 enum이 M1 시점(notes/local_docs)에 멈춤 | `_SEARCH_TOOL`에 6개 소스·`sender` 추가, 툴 루프가 `sender` 전달 |
| 출처 카드 발췌 | §8 "출처 카드" — 어떤 청크가 매칭됐는지 사용자가 볼 수 없음 | `Hit.snippet`(최고 점수 청크 앞부분), 카드에 표시 |
| 골든 평가 GUI | §1 성공 기준·§12 — 현재 CLI 전용(실 API 키·카운팅 우회) | 설정 탭 평가 절: `golden.yaml` 편집·실행·질문별 순위 표, `GET/PUT /api/eval/golden`, `POST /api/eval/golden/run` |
| 채팅 오류 표시 | 필터 검증 400이 일상화되는데 `ask()`가 비-2xx를 무시해 무증상 실패 | `ask()`가 `resp.ok` 검사 후 `detail` 표시, history 미오염 |

범위 밖: 검색 파라미터(RRF_K·CANDIDATES 등) GUI 튜닝, 평가 이력 저장, 재랭킹 모델, 필터 프리셋 저장, 답변 품질(LLM-judge) 평가.

**계획 지침**: 태스크 7개 이내 — ① 발췌(`Hit.snippet`) ② 툴 스키마+`sender` 전달+스트림 Fake 테스트 ③ `_apply_filters`+검증+`/api/chat` 통합+`filters_note` ④ 채팅 필터 UI+`ask()` 오류 표시 ⑤ `evaluate` 확장+CLI 기본값 ⑥ 골든 API+UI(한 태스크) ⑦ E2E+HANDOFF.

## 2. 채팅 필터

**UI** — 채팅 폼 아래 접이식 "필터" 행: 소스 체크박스 6개(기본 전부 해제 = 제한 없음), 기간 `date_from`/`date_to`(`<input type="date">`), 발신자 텍스트(헬프 텍스트: "입력 시 메일만 검색됩니다"). 값은 `filters` 객체로 `/api/chat` 페이로드에 포함: `{"source_filter": [...]|null, "date_from": "YYYY-MM-DD"|null, "date_to": ...|null, "sender": ...|null}`. 빈 값은 null. 질문 div 아래에 `textContent`로 한 줄: "필터(첫 검색 기준): 소스=notes · 2026-08-01~ · 발신자=… — 답변 근거는 Claude의 추가 검색으로 넓어질 수 있습니다".

**서버 검증** (`/api/chat`, `state["usage"].record("answer")` **이전**에 수행 — 400에는 answer를 계상하지 않는다; 위반 400):
- `source_filter`: null 또는 리스트; 각 원소는 `SOURCES` 안의 문자열; **중복 제거 후 `SOURCES` 순서로 정규화**(길이 ≤ 6); 빈 리스트는 null과 동일.
- `date_from`/`date_to`: null 또는 `datetime.date.fromisoformat`이 성공하는 문자열(`2026-13-45` 거부). `date_to`는 그대로 `search`에 넘긴다 — 자정 경계 보정은 `search.py`가 이미 한다.
- `sender`: null 또는 문자열 ≤ 200자(공백 trim, 빈 문자열은 null).
- **sender 조합 규칙**: `extra.sender`는 outlook_mail만 저장하므로 sender 필터는 사실상 메일 한정이다. `sender`가 있고 `source_filter`가 `outlook_mail`을 포함하지 않으면 400 "발신자 필터는 메일 소스에서만 동작합니다 — 소스에서 outlook_mail을 선택하거나 소스 선택을 비우세요". `source_filter`가 null이면 허용(서버가 `["outlook_mail"]`로 좁혀 적용하지는 않는다 — 검색이 자연히 메일만 남긴다).

**적용** — `web/app.py` 모듈 함수 `_apply_filters(search_fn, filters) -> search_fn'`:
- **선검색(강제)**: `answer_stream`은 사전 검색을 `search_fn(question)`으로만 호출한다(Fake·Claude 공통) → 키워드 인자가 전부 미지정이므로 필터가 그대로 들어간다.
- **툴 검색(기본값)**: 툴 루프는 4~5개 키워드를 항상 명시적으로 넘긴다 → **None 또는 falsy(`[]`, `""`)인 인자만 필터로 채운다**(Claude가 값을 준 인자는 우선). Claude가 `source_filter: []`를 돌려줘도 사용자 필터가 조용히 해제되지 않는다.
- 필터 적용 로직은 `web/app.py`에만 둔다 — `llm.py`의 **답변 루프 로직은 변경하지 않는다**(아래 스키마·고지 변경은 별도).

**Claude 고지** — 사용자 필터가 하나라도 있으면 `answer_stream(question, history, search_fn, filters_note="")`의 새 키워드 인자로 `"(사용자 필터 적용: 소스=notes, 기간=2026-08-01~ — 다른 범위가 필요하면 search 툴에 값을 명시하라)"`를 넘기고, `ClaudeAnswerer`는 사전 검색 결과 블록 앞에 그 줄을 붙인다. `FakeAnswerer`는 무시. Protocol에 기본값 있는 키워드 인자로 추가(기존 호출 호환).

**검색 툴 스키마** — `_SEARCH_TOOL.input_schema.properties.source_filter.items.enum`을 `["notes","local_docs","outlook_mail","outlook_cal","confluence","jira"]`로, `sender: {"type":"string","description":"보낸 사람 이메일 (메일 전용, 선택)"}` 추가, description에 "메일·일정·Confluence·Jira 포함" 반영. 툴 루프 `search_fn(...)` 호출에 `sender=args.get("sender")` 추가(현재 누락).

**대화 이력** — `history`에는 필터를 기록하지 않는다: 매 턴 현재 필터가 적용된다(M8 세션 저장 시 재검토).

**`ask()` 오류 표시** — `resp.ok`가 거짓이면 응답 JSON의 `detail`(없으면 상태코드)을 `answerDiv.textContent`에 "⚠️ …"로 표시하고 `history`에 push하지 않고 종료(기존 503 `_require_db` 경로도 함께 해소).

## 3. 출처 카드 발췌

- `models.Hit`에 `snippet: str = ""` 추가(끝에 기본값 — 기존 위치 인자 8개 생성자 호환: `search.py`, `tests/test_llm.py`).
- `search.search`: 문서별 최고 RRF 청크(`doc_best_chunk`, top 문서 전부에 존재)의 텍스트에서 헤더를 **정규식이 아니라 재구성으로** 제거: `header = f"[{title} | {updated_at[:10]}] "`, `text.removeprefix(header)`(불일치면 원문). 이후 공백·개행을 단일 공백으로 정규화하고 `SNIPPET_CAP = 200`자로 자른다. 승격 본문(`excerpt`)·LLM 컨텍스트(`_hits_block`)는 무변경.
- UI 카드: 제목 줄 아래 `<div class="snip">`에 `esc(h.snippet)`(빈 값이면 생략). CSS: `.snip { color:#666; font-size:.85em; white-space:normal; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden }` (`#messages`가 `pre-wrap`이라 `white-space:normal` 필수).
- `/api/chat` SSE `sources` 이벤트는 `asdict(h)`라 자동 포함.

## 4. 골든 평가 GUI

**파일** — `data_dir/golden.yaml` (gitignore 유지). 형식은 기존 CLI와 동일: `[{question, expect_source_id}]`. `expect_source_id`는 정확 일치 또는 경로 접미사(`_matches`). 헬프 텍스트: "`expect_source_id`는 전체 경로 또는 경로 접미사(파일명) — 동명 파일이 여러 폴더에 있으면 아무 쪽이나 적중으로 셉니다".

**파싱 규칙(공통)** — `yaml.safe_load`만 사용(`yaml.load`·`full_load` 금지). 결과 `None`은 빈 목록. 리스트가 아니면 400; 각 항목은 dict이고 `question`·`expect_source_id`가 비어 있지 않은 문자열이어야 함(위반 400 + 항목 번호). 케이스 수 상한 `GOLDEN_MAX_CASES = 50` — PUT·run 양쪽에서 초과 시 400.

**API**
- `GET /api/eval/golden` → `{"text": str, "path": str, "count": n}` — 파일 없으면 템플릿(주석 2줄 + 주석 처리된 예시 1건; `safe_load` 결과 None → `count: 0`).
- `PUT /api/eval/golden {"text"}` (로컬 오리진) → 파싱 규칙 검증, 원자적 저장(`.tmp`+`os.replace`), 256KB(UTF-8) 상한, `{"ok": true, "count": n}`. 템플릿을 그대로 저장해도 성공(`count: 0`).
- `POST /api/eval/golden/run` (로컬 오리진) → `_require_db`; `state["rebuilding"]`이면 409; 단일 실행 플래그 `state["evaluating"]`(non-blocking 락 패턴, 진행 중 409); 파일 없음·0건·파싱 실패 → 400 "golden.yaml에 질문을 먼저 작성하세요"/파싱 사유. **자체 읽기 커넥션**(`db.open_db(cfg.db_path)`)을 열어 `golden.evaluate(conn, state["embedder"], cases)` 실행 후 `finally`에서 닫는다(평가 중 재구축이 `read_conn`을 교체해도 안전). 카운팅 임베더를 거치므로 usage에 `embed`로 기록(질의 캐시 적중 시 미기록); 검색 경로이므로 일일 상한 게이트 대상 아님(상위 스펙 §10). 임베딩 호출 실패는 502 `{"detail": "임베딩 호출 실패: <예외 클래스명>"}` — 예외 메시지 본문(키 노출 가능)은 포함하지 않는다. 응답 `{"total", "hit_at_3", "rate", "target": 0.7, "pass": bool, "cases": [{"question", "expected", "rank": 1|2|3|null, "got": [상위 최대 3건 source_id]}]}`.
- `evaluate` 확장: 반환에 `cases`(질문별 순위) 추가; 기존 `misses`는 `cases`에서 `rank is None`인 항목으로 파생(키·형식 유지 — CLI 출력 호환). `rank`는 `got` 안에서 `_matches`가 처음 참인 인덱스+1.

**UI** — 설정 탭 "검색 품질 평가" 절: 경로, textarea(`.value`), [저장] [평가 실행] 버튼(실행 중 disable). 실행 전 confirm("최대 N건의 질의 임베딩 API 호출" + `indexing_allowed()`가 거짓이면 " — 이미 일일 상한 도달 상태, 추가 소모됩니다"; 이 값은 `/api/usage`로 조회). 결과: 헤더 "상위3 적중률 50% (1/2) — 목표 70% ❌" + 표(질문 / 기대 / 순위 / 상위 결과). 전부 `textContent`/`esc()`. `show('settings')`가 `loadGolden()`도 호출.

**CLI** — `python -m llmsearch.eval.golden`: `--golden`을 `required=False, default=None`으로 바꾸고 `load_config` 이후 `golden_path = args.golden or (cfg.data_dir / "golden.yaml")`. 파일이 없으면 "golden.yaml이 없습니다: <경로>" 출력 후 exit 1(현재는 트레이스백). 카운팅 우회 주석은 유지(실 API 직접 호출). 테스트는 `GeminiEmbeddings`(함수 내부 import)를 monkeypatch.

## 5. 오류 처리

| 상황 | 동작 |
|---|---|
| 필터 값 비정상(미지 소스·리스트 아님·날짜 형식·sender 과대) | 400, 검색·answer 계상 없음 |
| sender + outlook_mail 미포함 source_filter | 400 (안내 문구) |
| golden PUT 파싱 실패/항목 형식/50건 초과/256KB 초과 | 400 + 사유, 파일 미변경 |
| golden run: 파일 없음·0건·파싱 실패 | 400 |
| golden run: 진행 중 / 재구축 중 | 409 |
| golden run: 임베딩 실패 | 502 (클래스명만) |
| 스키마 불일치 상태 | `_require_db` 503 (chat·run) |
| chat/run 비-2xx | `ask()`·평가 UI가 `detail` 표시, history 미오염 |

## 6. 테스트

- 단위: `_apply_filters`(선검색 강제·툴 명시값 우선·None/`[]`/`""` 채움), `/api/chat` 필터 검증(400 6종·정규화·answer 미계상)·정상 적용(notes 필터 시 local_docs 문서 미포함 — `make_app_with_docs`+notes 조합)·`filters_note` 전달(FakeAnswerer가 받은 값 기록), `_SEARCH_TOOL` enum·`sender`, `ClaudeAnswerer` 툴 루프가 `sender`를 `search_fn`에 전달하고 `filters_note`를 사전 검색 블록 앞에 붙임 — **`messages.stream` 컨텍스트 매니저 Fake를 신규 작성**(`text_stream` 이터레이터 + `get_final_message()`가 1회차 `stop_reason="tool_use"`·tool_use 블록 1개, 2회차 `end_turn`), `Hit.snippet`(헤더 재구성 제거·`]`·`|` 포함 제목·공백 정규화·200자·최고 청크 기준)·기존 위치 인자 호환, golden GET 템플릿·PUT 검증(None→0건, 비리스트 400, 항목 형식 400, 51건 400)·원자성·run 결과(rank·pass·misses 파생)·0건 400·409(진행 중/재구축 중)·502 클래스명만·오리진 403, `evaluate` `cases` 확장 + `misses` 호환, CLI `--golden` 기본값·파일 없음 exit 1.
- E2E(9.9 뒤, `# 10.` 앞; 기존 66건 기대값 무변경, 신규 N건 추가 → 66+N): 새 카드 단언은 `page.locator('.msg-a').last.locator('.src')`로 스코프(#messages 누적 함정), 폼 버튼은 `form >> text=검색`. (a) 필터 행에서 notes만 선택 → 질문 → 마지막 답변의 카드 전부 notes + 필터 표시 줄 존재; (b) 카드에 `.snip` 존재·비어 있지 않음; (c) 설정 탭 평가 절에 골든 2건 저장 — 적중 1건(`프로젝트A 킥오프 언제?` → `kickoff.md`) + 확정 미스 1건(`존재하지 않는 주제 XYZQW` → `none.md`) → 실행 → 헤더 정확히 `50% (1/2)`·❌, 표 2행. 예산: 채팅 2 + 평가 임베딩 ≤2 ≈ +4 (≈34 < 50).

## 7. 결정 기록

| 결정 | 근거 |
|---|---|
| 필터는 선검색 강제·툴 검색 기본값(None/falsy만 채움) + Claude 고지 | 사용자 범위 제한과 Claude의 다중 홉 탐색 자유를 동시에 보존; 고지 없이는 Claude가 우회 필요성을 모름 (리뷰 I1) |
| 필터 적용 로직은 web 계층, llm.py는 스키마·`sender`·`filters_note`만 | 답변 루프 로직 불변 (리뷰 C1 정정) |
| sender + 비메일 소스 조합은 400 | 조용한 0건 대신 원인 안내 (리뷰 C2) |
| 발췌는 최고 청크 200자, LLM 컨텍스트 무변경 | 사용자 확인용 표시이지 답변 입력이 아님 |
| 골든 평가를 GUI에서 카운팅 임베더로, 50건 상한 | 앱 예산 안에서 측정; 1클릭 폭주 방지 (리뷰 I4) |
| 평가는 자체 읽기 커넥션 | 재구축의 커넥션 교체와 격리 |
| 평가 결과는 저장하지 않음 | 회귀 비교는 사용자 수동(M8 이후 후보) |
| 필터 성능 | 필터 시 허용 chunk id를 전수 물질화·sender는 documents 스캔 — 개인 규모(수만 청크)에서 수용 |
