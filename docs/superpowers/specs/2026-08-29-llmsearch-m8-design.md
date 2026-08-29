# llmsearch M8 — 채팅 UX 설계

> 상위 스펙: `2026-08-17-llmsearch-design.md` §3(인덱스는 소모품), §8 답변 플로우 5("후속 질문: 세션 내 대화 이력을 Claude 컨텍스트에 포함"), §13(데이터 폴더). 로드맵: `2026-08-29-llmsearch-roadmap-m6-m9.md`.
> 목표: 대화가 새로고침에 사라지지 않게 세션으로 저장·복원하고, 답변을 md로 내보내며, 출처 본문을 카드에서 바로 미리본다. 새 외부 의존성 없음.

## 1. 범위

| 기능 | 근거 | 산출물 |
|---|---|---|
| 대화 세션 저장/목록/불러오기 | §8-5 후속 질문 이력 — 현재는 브라우저 메모리(`history` 배열)라 새로고침에 소실 | `chats.py` `ChatStore`(별도 `data_dir/chats.db`), `/api/chats` CRUD, `/api/chat`의 `session_id`, 채팅 탭 세션 목록 |
| 답변 내보내기 | 로드맵 M8 — 답변+출처를 md로 보관, notes 인덱싱 옵션 | `POST /api/chats/{id}/export` → `data_dir/exports/*.md`, `chat.export_to_notes` 설정 |
| 출처 본문 미리보기 | 로드맵 M8 — 카드에서 원본 열기 없이 근거 확인 | 카드 [미리보기] → `<dialog>`에 승격 본문(`Hit.excerpt`, 클라이언트 보유) 표시 — 신규 엔드포인트 없음 |

범위 밖: 세션 검색/태그, 답변 재생성, 메시지 편집, 다중 사용자, 내보내기 형식 선택(md 고정), 세션 자동 요약 제목(첫 질문 절단으로 충분).

**계획 지침**: 태스크 ≤ 6 — ① `ChatStore` ② `/api/chats` CRUD + `/api/chat` 세션 통합 ③ 내보내기 API + notes 옵션 ④ UI(세션 목록·복원·자동 생성·미리보기 dialog) ⑤ E2E+HANDOFF.

## 2. 저장소 — `chats.db`

- 위치 `data_dir/chats.db`(§13에 추가). **인덱스와 분리**: 인덱스는 소모품이라 rebuild가 지우지만 대화는 사용자 산출물 — `rebuild.py`는 이 파일을 절대 건드리지 않는다. gitignore는 data_dir 자체가 외부라 해당 없음.
- `chats.py` `ChatStore(path)`: 자체 `sqlite3.connect(check_same_thread=False)`, WAL, `meta(schema_version='1')`, 자체 `threading.Lock`으로 쓰기 직렬화(웹 스레드풀).
  ```sql
  CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, title TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,
      session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
      sources_json TEXT NOT NULL DEFAULT '[]', filters_json TEXT NOT NULL DEFAULT 'null',
      created_at TEXT NOT NULL);
  CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
  ```
- API(파이썬): `create_session(title) -> int`; `list_sessions(limit=50) -> [{id,title,created_at,updated_at,message_count}]` (updated_at 내림차순); `get_session(id) -> {id,title,created_at,updated_at,messages:[{id,role,content,sources,filters,created_at}]}` (없으면 `KeyError`); `append(session_id, role, content, sources=None, filters=None) -> int` (세션 `updated_at` 갱신; 없으면 `KeyError`); `history(session_id, limit=20) -> [{role,content}]` (마지막 limit개, Claude 컨텍스트용 — `sources` 제외); `delete_session(id) -> bool`; `export_markdown(id) -> str`; `close()`.
- 제목: 첫 질문을 공백 정규화 후 60자 절단(빈 문자열이면 "새 대화"). 제목 변경 API는 범위 밖.
- `sources`는 `Hit` dict 목록(`asdict`, `excerpt` ≤ 6000자 포함 — 미리보기·복원용). 메시지당 최대 12건이라 개인 규모에서 수용.
- 스키마 불일치 시(`schema_version != '1'`): `RuntimeError` — `create_app`은 이를 잡아 `state["chat_store"] = None`, `state["chat_store_error"]`에 메시지 보관, 세션 API는 503, 채팅은 세션 없이(무저장) 계속 동작. 배너는 범위 밖(로그·503 메시지로 충분).

## 3. API

**세션**
- `GET /api/chats` → `[{id,title,created_at,updated_at,message_count}]`(≤50).
- `POST /api/chats {"title"?: str}` (로컬 오리진) → `{"id", "title"}` — 제목 미지정이면 "새 대화"(첫 질문 저장 시 제목이 "새 대화"면 첫 질문으로 교체).
- `GET /api/chats/{id}` → 세션 + 메시지(`sources` 포함); 없으면 404. `id`는 경로 파라미터 `int`(FastAPI 검증).
- `DELETE /api/chats/{id}` (로컬 오리진) → `{"ok": true}`; 없으면 404.
- `chat_store`가 None이면 위 전부 503 `chat_store_error`.

**`/api/chat` 세션 통합**
- 페이로드에 `session_id: int|null` 추가. 검증(`_validate_filters` 직후, `record("answer")` 이전): int가 아니거나 존재하지 않는 세션이면 404 "세션을 찾을 수 없습니다"; `chat_store`가 None인데 `session_id`가 오면 503.
- `session_id`가 있으면 **서버가 `store.history(session_id)`로 이력을 구성**하고 페이로드 `history`는 무시한다(진실 원천 단일화). 없으면 기존처럼 페이로드 `history` 사용(무저장 — 테스트·`page.request` 호환).
- 저장: 스트림이 끝난 뒤(`done` 직전) `append(session_id,"user",question,filters=filters)` → `append(session_id,"assistant",answer_text,sources=hits)`. `answer_text`는 `text` 이벤트 누적, `error` 이벤트는 `"\n⚠️ "+message`로 본문에 합침(클라이언트 표시와 동일). `sources`는 `sources` 이벤트의 hits(`asdict`). 세션 제목이 "새 대화"면 첫 질문으로 갱신. 저장 실패는 로그만(스트림은 이미 전송됨) — `_logger.exception` 대신 클래스명만 기록.
- 응답 SSE에 `event: saved\ndata: {"session_id": N}` 추가(저장 성공 시, `done` 앞) — UI가 목록을 갱신할 신호.

**내보내기**
- `POST /api/chats/{id}/export` (로컬 오리진) → `{"ok": true, "path": str}`. `data_dir/exports/` 생성 후 `YYYYmmdd-HHMMSS-<slug>.md` 작성(원자적 tmp+replace). `slug` = 제목을 `_sanitize_segment`와 같은 규칙으로(영숫자·한글·`-_` 외 `_`, 40자). 없으면 404. 내용:
  ```
  # <제목>
  - 생성: <created_at> · 내보내기: <now>
  ## Q1. <질문>
  (필터: ...)            ← filters가 있을 때만
  <답변>
  출처:
  - [notes] 제목 — url_or_path
  ## Q2. ...
  ```
- `chat.export_to_notes: bool = false`(config.yaml, `Config.export_to_notes`): true면 `run_sync("notes")`가 `exports_dir`를 notes 폴더에 추가(`folders = cfg.notes_folders + [cfg.exports_dir]`), 즉 내보낸 대화가 검색 대상이 된다. `Config.exports_dir` 프로퍼티 = `data_dir/"exports"`. README·config.example.yaml에 항목.

## 4. UI

- 채팅 탭 상단 한 줄: `<select id="sessionSelect">`(세션 목록, "— 새 대화 —" 첫 항목) + [새 대화] [삭제] [내보내기] 버튼. 목록은 탭 진입·저장 이벤트마다 `GET /api/chats`로 갱신(`textContent`/`option.text`).
- **자동 생성**: 질문 전송 시 활성 세션이 없으면 `POST /api/chats`로 만들고 `session_id`를 페이로드에 넣는다. 클라이언트 `history` 배열은 세션이 있으면 보내지 않는다(서버가 구성). `chat_store`가 503이면 세션 없이 기존 방식(클라이언트 history)으로 폴백하고 `#chatNote`에 "대화 저장 불가: …" 표시.
- **복원**: 세션 선택 → `GET /api/chats/{id}` → `#messages`를 비우고 메시지를 순서대로 렌더(질문 `.msg-q`, 필터 줄 `.filters-note`, 답변 `.msg-a` + 출처 카드 `.src`를 저장된 `sources`로 동일 템플릿 렌더). 렌더 함수 `renderSources(answerDiv, hits)`를 `ask()`와 공유(중복 제거).
- **미리보기**: 카드에 [미리보기] 버튼 → `<dialog id="preview">`에 제목(`textContent`)·메타·본문(`<pre>` `textContent`로 `h.excerpt`) 표시, [닫기]. `excerpt`는 이미 SSE로 내려오는 값(≤6000자) — 신규 요청 없음. `hits`는 `renderSources`가 카드 요소에 인덱스로 보관(`data-i`)하고 배열은 클로저/`answerDiv._hits`로 유지.
- 삭제: confirm 후 `DELETE`, 목록 갱신, `#messages` 비움. 내보내기: `POST export` → alert에 경로.
- 필터는 `history`에 저장되지 않는 원칙 유지(M7 결정) — 저장된 메시지의 `filters`는 표시용.

## 5. 오류 처리

| 상황 | 동작 |
|---|---|
| `session_id` 비정상/미존재 | 404, answer 미계상 |
| `chats.db` 스키마 불일치/열기 실패 | 기동 성공, 세션 API 503, 채팅은 무저장 폴백 |
| 저장 실패(디스크 등) | 스트림은 정상, `saved` 이벤트 없음, 로그 클래스명 |
| export 대상 없음 / exports 쓰기 실패 | 404 / 500 `{"detail": "내보내기 실패: <클래스명>"}` |
| 비로컬 Origin | 403 (POST/DELETE 전부) |

## 6. 테스트

- 단위 `tests/test_chats.py`: 생성/목록 정렬/조회/append+updated_at/history limit·순서·sources 제외/삭제 cascade/제목 절단·공백 정규화/export_markdown 형식(필터 줄 조건부)/스키마 불일치 RuntimeError/동시 append 락.
- 웹: CRUD 200/404/403/503; `/api/chat`에 `session_id` → 서버 이력 사용(FakeAnswerer가 받은 history 길이 확인 — `FakeAnswerer.last_history` 관찰 필드 추가), 저장된 user/assistant 메시지·sources·`saved` 이벤트, 제목 갱신; `session_id` 없으면 무저장 + 페이로드 history 전달; 잘못된 `session_id` 404 + answer 미계상; export 파일 생성·내용·slug·원자성; `export_to_notes` true면 notes 동기화가 exports를 인덱싱(FakeEmbeddings); rebuild가 chats.db를 보존.
- E2E(9.11 뒤, `# 10.` 앞; 기존 73건 무변경): (a) 질문 전송 → `#sessionSelect`에 세션 1개 생성·제목=질문; (b) 새로고침(`page.reload()`) → 세션 선택 → 메시지·카드 복원(`.msg-a` 1개 이상, `.src` ≥1); (c) 카드 [미리보기] → `#preview[open]`에 본문 텍스트 존재 → 닫기; (d) [내보내기] → alert 경로 → `.e2e-data/data/exports/*.md` 존재·질문 포함. 예산: 채팅 1회(+2) ≈ 36 < 50. 새로고침 후 `dialogs` 핸들러는 `page.on`이라 유지됨.

## 7. 결정 기록

| 결정 | 근거 |
|---|---|
| 별도 `chats.db` | 인덱스는 소모품(rebuild 삭제 대상), 대화는 사용자 산출물 — 파일 분리로 재구축 경로가 구조적으로 못 건드림 |
| 서버가 이력 구성(세션 있을 때) | 진실 원천 단일화; 새로고침 후 후속 질문이 이전 맥락을 자동으로 잇는다 |
| 세션 없는 호출은 기존 방식 유지 | 테스트·E2E `page.request`·CLI 호환, 저장 비용 없음 |
| 미리보기는 클라이언트 보유 `excerpt` | 이미 SSE로 전달되는 값 — 엔드포인트·재조회 불필요(YAGNI) |
| 내보내기는 세션 단위 md | 답변 단위는 복사로 충분; notes 인덱싱 옵션으로 "내가 물어본 것"도 검색 가능 |
| 스키마 불일치 시 무저장 폴백 | 채팅 기능 자체가 저장소 장애에 볼모가 되지 않게 |
