# llmsearch M8 — 채팅 UX 설계

> 상위 스펙: `2026-08-17-llmsearch-design.md` §3(인덱스는 소모품), §8 답변 플로우 5("후속 질문: 세션 내 대화 이력을 Claude 컨텍스트에 포함"), §13(데이터 폴더). 로드맵: `2026-08-29-llmsearch-roadmap-m6-m9.md`.
> 목표: 대화가 새로고침에 사라지지 않게 세션으로 저장·복원하고, 답변을 md로 내보내며, 출처 본문을 카드에서 바로 미리본다. 새 외부 의존성 없음.
> 3관점 리뷰(2026-08-29) 반영본 — Critical 2·Important 10·Minor 8 전부 반영.

## 1. 범위

| 기능 | 근거 | 산출물 |
|---|---|---|
| 대화 세션 저장/목록/불러오기 | §8-5 후속 질문 이력 — 현재는 브라우저 메모리(`history` 배열)라 새로고침에 소실 | `chats.py` `ChatStore`(별도 `data_dir/chats.db`), `/api/chats` CRUD, `/api/chat`의 `session_id`, 채팅 탭 세션 목록 |
| 답변 내보내기 | 로드맵 M8 — 답변+출처를 md로 보관, notes 인덱싱 옵션 | `POST /api/chats/{id}/export` → `data_dir/exports/chat-<id>-<slug>.md`, `chat.export_to_notes` 설정 |
| 출처 본문 미리보기 | 로드맵 M8 — 카드에서 원본 열기 없이 근거 확인 | 카드 [미리보기] → `<dialog>`에 승격 본문(`Hit.excerpt`, 클라이언트 보유) 표시 — 신규 엔드포인트 없음 |

범위 밖: 세션 검색/태그, 답변 재생성, 메시지 편집, 다중 사용자, 내보내기 형식 선택(md 고정), 세션 자동 요약 제목(첫 질문 절단으로 충분), 세션 목록 페이지네이션(50건 초과분은 목록에 안 보일 뿐 삭제되지 않음 — M9 후보).

**계획 지침**: 태스크 ≤ 6 — ① `ChatStore` ② `/api/chats` CRUD + `/api/chat` 세션 통합 ③ 내보내기 API + notes 옵션 ④ UI(세션 목록·복원·자동 생성·미리보기 dialog) ⑤ E2E + HANDOFF + 상위 스펙 §13 갱신(chats.db·exports/).

## 2. 저장소 — `chats.db`

- 위치 `data_dir/chats.db`. **인덱스와 분리**: 인덱스는 소모품이라 rebuild가 지우지만 대화는 사용자 산출물 — `rebuild.py`는 이 파일을 절대 건드리지 않는다(`reset_index`·`recover_schema_mismatch`는 `index.db{,-wal,-shm}`만 다룸 — 구조적 격리).
- `chats.py` `ChatStore(path)`: 자체 `sqlite3.connect(check_same_thread=False)`, **`PRAGMA journal_mode=WAL`과 `PRAGMA foreign_keys=ON`을 함께 실행**(커넥션 단위 설정 — `db.open_db`와 동일), 자체 `threading.Lock`으로 **읽기·쓰기 모두** 직렬화(단일 커넥션을 웹 스레드풀이 공유하므로 미커밋 중간 상태 노출 방지).
  ```sql
  CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
      sources_json TEXT NOT NULL DEFAULT '[]', filters_json TEXT NOT NULL DEFAULT 'null',
      created_at TEXT NOT NULL);
  CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
  ```
  `AUTOINCREMENT`는 rowid 재사용을 막는다 — 삭제한 세션의 id가 새 세션에 재배정되어 남은 메시지가 "부활"하는 경로 차단.
- 열기 순서: 테이블 생성 → `SELECT value FROM meta WHERE key='schema_version'` → **행이 없으면 `'1'` 삽입·커밋**, 있는데 `'1'`이 아니면 `RuntimeError`(`db.open_db`와 동형).
- API(파이썬): `create_session(title="새 대화") -> int`; `set_title(session_id, title) -> None`(없으면 `KeyError`, `updated_at` 갱신); `list_sessions(limit=50) -> [{id,title,created_at,updated_at,message_count}]`(updated_at 내림차순); `get_session(id) -> {id,title,created_at,updated_at,messages:[{id,role,content,sources,filters,created_at}]}`(없으면 `KeyError`); `append(session_id, role, content, sources=None, filters=None) -> int`(세션 `updated_at` 갱신; 없으면 `KeyError`); `history(session_id, limit=20, max_chars=40_000) -> [{role,content}]`(Claude 컨텍스트용 — `sources` 제외; 마지막 `limit`개를 취한 뒤 **선두가 `assistant`면 버린다**(Messages API는 첫 메시지가 `user`여야 함), 누적 `content` 길이가 `max_chars`를 넘으면 오래된 쌍부터 제외); `delete_session(id) -> bool`(cascade에 의존하지 않고 같은 트랜잭션에서 `DELETE FROM messages WHERE session_id=?` → `DELETE FROM sessions WHERE id=?` 2계층); `export_markdown(id) -> str`; `close()`(`create_app`의 shutdown 핸들러가 호출).
- 제목: 공백 정규화 후 60자 절단(빈 문자열이면 "새 대화"). `POST /api/chats`의 `title`은 200자 초과 시 400.
- `sources`는 `Hit` dict 목록(`asdict`, `excerpt` ≤ 6000자 포함 — 미리보기·복원용). 메시지당 최대 12건이라 개인 규모에서 수용; 긴 세션의 `GET /api/chats/{id}`는 수 MB가 될 수 있다(페이지네이션은 M9 후보).
- **기동 격리**: `create_app`은 `ChatStore` 생성의 **모든 예외(`Exception`)**를 잡아 `state["chat_store"]=None`, `state["chat_store_error"]=type(exc).__name__`로 보관하고 기동을 계속한다(경로·예외 문자열은 로그에만). 세션 API는 503, 채팅은 세션 없이(무저장) 계속 동작. 배너는 범위 밖.

## 3. API

**세션** (`chat_store`가 None이면 전부 503 `{"detail": "대화 저장소를 열 수 없습니다: <클래스명>"}`)
- `GET /api/chats` → `[{id,title,created_at,updated_at,message_count}]`(≤50).
- `POST /api/chats {"title"?: str}` (로컬 오리진) → `{"id","title"}` — 미지정/빈 값이면 "새 대화"; 문자열 아님·200자 초과 400.
- `GET /api/chats/{id}` → 세션 + 메시지(`sources` 포함); 없으면 404. `id`는 경로 파라미터 `int`.
- `DELETE /api/chats/{id}` (로컬 오리진) → `{"ok": true}`; 없으면 404.

**`/api/chat` 세션 통합**
- 페이로드 `session_id: int|null` 추가. 검증(`_validate_filters` 직후, `record("answer")` 이전): **`bool`을 제외한 `int`**(`isinstance(v, int) and not isinstance(v, bool)`)가 아니거나 존재하지 않는 세션이면 404 "세션을 찾을 수 없습니다"; `chat_store`가 None인데 `session_id`가 오면 503.
- 순서: ① `session_id`가 있으면 **`hist = store.history(session_id)`를 먼저 확정**(현재 질문이 이력에 중복 포함되지 않게) — 페이로드 `history`는 무시(진실 원천 단일화); 없으면 페이로드 `history` 사용(무저장 — 테스트·`page.request` 호환). ② 스트림 시작 전에 `append(session_id, "user", question, filters=filters)`; 세션 제목이 "새 대화"면 `set_title(첫 질문)`. ③ assistant 저장은 `event_stream`의 **`try/finally`**에서 수행 — 정상 종료·클라이언트 중단(`GeneratorExit`) 모두 부분 답변이라도 보존한다. `finally` 안에서는 절대 `yield`하지 않는다. `answer_text`는 `text` 이벤트 누적, `error` 이벤트는 `"\n⚠️ "+message`로 본문에 합침. `sources`는 `sources` 이벤트의 hits(`asdict`). 저장 실패는 로그(클래스명)만.
- `event: saved\ndata: {"session_id": N}`는 **정상 종료 경로에서만** `done` 앞에 보낸다 — UI가 목록을 갱신할 신호.

**내보내기**
- `POST /api/chats/{id}/export` (로컬 오리진) → `{"ok": true, "path": str}`. 없으면 404. `data_dir/exports/` 생성 후 **세션 단위 결정적 파일명 `chat-<id>-<slug>.md`**로 작성(tmp+`os.replace`; 재내보내기는 같은 파일 갱신 — notes 재인덱싱 시 mtime 변경으로 1건만 갱신, 준중복 누적 없음).
- `slug` = `summarize._sanitize_segment(title)` 적용 후 `[^0-9A-Za-z가-힣\-_]`를 `_`로 치환, 40자 절단, 빈 문자열이면 `chat`. 작성 직전 `(exports_dir / name).resolve().relative_to(exports_dir.resolve())`로 2계층 재검증, 실패 시 500 `{"detail": "내보내기 실패: 경로 검증"}`. 쓰기 실패 500 `{"detail": "내보내기 실패: <클래스명>"}`.
- 내용(자기참조 오염 방지 — LLM 답변이 1차 출처로 재인용되지 않게 제목 접두어·고지 고정):
  ```
  # [대화기록] <제목>
  > 이 문서는 llmsearch가 생성한 답변 기록입니다 — 1차 출처가 아닙니다. 원 출처는 각 답변 하단 목록을 확인하세요.
  - 생성: <created_at> · 내보내기: <now>

  ## Q1. <질문>
  (필터: 소스=notes · 기간=...)     ← filters가 있을 때만
  <답변>

  출처:
  - [notes] 제목 — url_or_path

  ## Q2. ...
  ```
- `chat.export_to_notes: bool = false`(config.yaml → `Config.export_to_notes`): true면 `run_sync("notes")`가 `cfg.exports_dir`를 notes 폴더에 추가(`folders = cfg.notes_folders + [cfg.exports_dir]`; 기존 sid dedupe로 `notes_folders`가 data_dir를 포함해도 중복 없음). `Config.exports_dir` 프로퍼티 = `data_dir / "exports"`. README·config.example.yaml에 항목.

## 4. UI

- 채팅 탭 상단 한 줄: `<select id="sessionSelect">`("— 새 대화 —" 첫 항목 + 세션 목록, `option.text`로 제목) + [새 대화] [삭제] [내보내기] 버튼 + `<span id="chatNote">`. `loadSessions()`는 탭 진입·`saved` 이벤트·삭제·[새 대화] 후에 호출하고, **스크립트 말미에서 `loadStatus()`와 함께 1회 호출**한다(채팅 탭이 기본 활성이라 `show('chat')`이 발화하지 않는다).
- **자동 생성**: 질문 전송 시 활성 세션이 없으면 `POST /api/chats`로 만들고 `session_id`를 페이로드에 넣는다(클라이언트 `history`는 세션이 있으면 보내지 않는다). `POST /api/chats`가 503이면 세션 없이 기존 방식(클라이언트 `history`)으로 폴백하고 `#chatNote`에 "대화 저장 불가: <detail>" 표시.
- **[새 대화]**: 활성 세션을 해제하고 `#messages`와 클라이언트 `history` 배열을 비운다(다음 질문에서 자동 생성).
- **복원**: 세션 선택 → `GET /api/chats/{id}` → `#messages`·클라이언트 `history` 배열을 비우고 메시지를 순서대로 렌더 — 질문 `.msg-q`·필터 줄 `.filters-note`·답변 `.msg-a`는 **`textContent`로만**, 출처 카드는 `ask()`와 공유하는 `renderSources(answerDiv, hits)`(내부에서 `esc()` 적용, 신규 이스케이프 규칙 없음). 폴백(503) 경로가 다른 세션의 이력을 전송하지 않도록 복원·[새 대화] 시 `history` 배열을 반드시 비운다.
- **미리보기**: 카드에 [미리보기] 버튼 → `<dialog id="preview">`에 제목·메타(`textContent`)·본문(`<pre id="previewBody">` `textContent`로 `h.excerpt`) 표시, [닫기]. `excerpt`는 이미 SSE로 내려오는 값(≤6000자) — 신규 요청 없음. `renderSources`는 `hits` 배열을 `answerDiv._hits`에 보관하고 버튼은 `data-i` 인덱스로 참조.
- 삭제: confirm 후 `DELETE`, 목록 갱신, `#messages`·`history` 비움. 내보내기: `POST export` → alert에 경로.
- 필터는 `history`에 저장되지 않는 원칙 유지(M7 결정) — 저장된 메시지의 `filters`는 표시용.

## 5. 오류 처리

| 상황 | 동작 |
|---|---|
| `session_id` 비정상(bool 포함)/미존재 | 404, answer 미계상 |
| `POST /api/chats` title 비문자열/200자 초과 | 400 |
| `chats.db` 스키마 불일치/손상/권한 오류 | 기동 성공, 세션 API 503(클래스명), 채팅은 무저장 폴백 |
| 스트림 중 클라이언트 중단 | user 메시지는 이미 저장, assistant 부분 답변 `finally`에서 저장, `saved` 이벤트 없음 |
| 저장 실패(디스크 등) | 스트림은 정상, `saved` 이벤트 없음, 로그 클래스명 |
| export 대상 없음 / 경로 검증 실패 / 쓰기 실패 | 404 / 500 / 500(클래스명) |
| 비로컬 Origin | 403 (POST/DELETE 전부) |

## 6. 테스트

- 단위 `tests/test_chats.py`: 생성/목록 정렬/조회/append+updated_at/`set_title`/`history` limit·순서·sources 제외·**선두 assistant 제거·max_chars 초과 시 오래된 쌍 탈락**/삭제 2계층(+ **삭제 후 새 세션 id 재사용 없음, 이전 메시지 0건**)/제목 절단·공백 정규화/`export_markdown` 형식(접두어·고지·필터 줄 조건부)/스키마 버전 행 생성·불일치 RuntimeError/동시 append 락.
- 웹: CRUD 200/400/404/403/503; `/api/chat`에 `session_id` → 서버 이력 사용(`FakeAnswerer.last_history` 관찰 필드 추가 — 페이로드 history 무시 확인), user가 스트림 전에 저장·assistant가 스트림 후 저장·sources·`saved` 이벤트, 제목 갱신; 클라이언트 중단 시뮬레이션(제너레이터 `close()`) → assistant 부분 저장·`saved` 없음; `session_id` 없으면 무저장 + 페이로드 history 전달; `session_id` `true`/미존재 404 + answer 미계상; `chat_store` None이면 503·채팅은 계속; export 파일명 결정성·재내보내기 덮어쓰기·내용(접두어·고지)·slug(경로 탈출 문자)·2계층 검증; `export_to_notes` true면 notes 동기화가 exports를 인덱싱(제목 `[대화기록] …`); rebuild가 chats.db를 보존.
- E2E(9.11 뒤, `# 10.` 앞; 기존 73건 무변경): (a) 채팅 탭에서 **[새 대화]**를 눌러 명시 생성 후 질문 전송 → `#sessionSelect` 옵션 수가 이전보다 1 증가, 선택된 옵션 제목이 방금 질문과 같음(앞선 UI 채팅들이 이미 세션 1개를 만들어 두므로 "총 1개"는 단정하지 않는다); (b) `page.reload()` → `#sessionSelect option` 수 ≥ 2까지 대기 → 해당 세션 `select_option` → `.msg-a` ≥1·`.src` ≥1 복원; (c) 카드 [미리보기] → `#preview[open]`의 `#previewBody` 텍스트 비어 있지 않음 → 닫기; (d) [내보내기] → alert 경로 → `.e2e-data/data/exports/chat-*.md` 존재·질문·`[대화기록]` 포함. 예산: 채팅 1회(+2) ≈ 38 < 50. `page.on("dialog")`는 reload 후 유지.

## 7. 결정 기록

| 결정 | 근거 |
|---|---|
| 별도 `chats.db` | 인덱스는 소모품(rebuild 삭제 대상), 대화는 사용자 산출물 — 파일 분리로 재구축 경로가 구조적으로 못 건드림 |
| `AUTOINCREMENT` + FK PRAGMA + 2계층 삭제 | rowid 재사용·cascade 미작동으로 삭제된 대화가 새 세션에 부활하는 경로 차단 (리뷰 C1) |
| ChatStore 열기 실패는 무저장 폴백 | 채팅 기능이 저장소 장애에 볼모가 되지 않게 — index.db 스키마 불일치 처리와 동급 (리뷰 C2) |
| user 선저장 + assistant `finally` 저장 | 답변 중 새로고침이 M8이 겨냥한 행동 — 부분이라도 보존 (리뷰 I1) |
| history 선두 user 보장 + 문자 상한 | Messages API 400 방지, 토큰 비용 상한(호출 수 게이트는 토큰을 못 본다) (리뷰 I2) |
| 서버가 이력 구성(세션 있을 때) | 진실 원천 단일화; 새로고침 후 후속 질문이 이전 맥락을 자동으로 잇는다 |
| 세션 없는 호출은 기존 방식 유지 | 테스트·E2E `page.request`·CLI 호환, 저장 비용 없음 |
| 미리보기는 클라이언트 보유 `excerpt` | 이미 SSE로 전달되는 값 — 엔드포인트·재조회 불필요(YAGNI) |
| 내보내기 파일명 세션 단위 결정적 + `[대화기록]` 접두어·고지 | 재내보내기 준중복 누적 방지; notes 인덱싱 시 LLM 답변이 1차 출처로 재인용되는 자기참조 오염 억제 (리뷰 I6·I9) |
