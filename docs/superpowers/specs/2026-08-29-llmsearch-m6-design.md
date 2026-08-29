# llmsearch M6 — 운영 완성 설계 (스펙 잔여 P1)

> 상위 스펙: `2026-08-17-llmsearch-design.md` §3 §8 §9 §10. 로드맵: `2026-08-29-llmsearch-roadmap-m6-m9.md`.
> 목표: 상위 스펙에 명시됐지만 미구현인 운영 기능을 채운다. 새 외부 의존성 없음.
> 3관점 리뷰(2026-08-29) 반영본 — Critical 4·Important 11·Minor 10 전부 반영. 리뷰가 잡은 핵심: 초안은 기존 코드의 `prior_map`/`_place`/삭제 판정을 오독해 "요약 md 재사용"을 정반대로(중복 생성·삭제·전량 재요약) 동작시킬 뻔했다.

## 1. 범위와 분할

한 계획으로는 과대(실 태스크 12개)라 **두 계획으로 나눈다**. M6a는 단독으로 유용하고 위험이 낮다. M6b에 위험(인덱스 초기화·요약 md 보존)이 집중되므로 리뷰 밀도를 높인다. 로드맵의 "M9는 M6 rebuild에 의존"은 M6b에만 걸린다.

| 계획 | 기능 | 근거 |
|---|---|---|
| **M6a** | **선행 리팩터**(DB 커넥션을 `state`에서 호출 시점 조회, `_require_db` 가드, `run_sync`의 conn 획득을 락 내부로, 스케줄러 예외 격리 — M6b 스키마 불일치 경로의 전제이며 단독으로도 무해), 설정 탭 rules.md 편집(+요약 규칙 주입, notes 인덱싱), 재요약(문서별/전체), 사용량 GUI 표시, 상태 변경 API의 로컬 오리진 검사 | §9, §10 |
| **M6b** | rebuild(제자리 초기화 + 요약 md 재사용), 스키마 불일치 기동·배너·복구, CLI `--rebuild` | §8, §3 "인덱스는 소모품, 요약 md는 보존" |

범위 밖: 사용량 차트, rules.md 문법 검증, 재요약 진행률, 요약 모델 선택 UI, 소스별 부분 rebuild, local_docs의 미스 카운터(보수적 삭제 판정 — 별도 과제로 기록).

## 2. 공통 — 상태 변경 API 보호 (M6a)

`/api/rebuild`·`/api/resummarize`는 인증 없이 실비용·인덱스 초기화를 일으키므로, 임의 웹페이지의 `fetch(..., {mode:'no-cors'})` 단순 요청으로 원격 트리거되면 안 된다(기존 `/api/sync`·`/api/archive`도 같은 노출). 공통 FastAPI 의존성 `_local_origin_only`:
- `Origin`(없으면 `Referer`) 헤더가 있으면 `http://127.0.0.1[:port]`·`http://localhost[:port]`만 허용, 아니면 403. 브라우저는 크로스오리진 POST/PUT/DELETE(no-cors 단순 요청 포함)에 항상 `Origin`을 붙이므로 이것만으로 CSRF가 차단된다. 헤더가 둘 다 없는 요청(curl·CLI·TestClient)은 통과. (초안의 JSON Content-Type 415 요구는 중복이고 바디 없는 `/api/sync` 호출을 깨뜨려 폐기 — 계획 시점 ruling)
- 적용: 상태를 바꾸는 모든 POST/PUT/DELETE — `/api/sync/{source}`, `/api/archive`, `/api/atlassian/register`·`registrations(DELETE)`, `/api/rules(PUT)`, `/api/resummarize`, `/api/rebuild`. `/api/open`·`/api/chat`은 기존 방어 유지(chat은 JSON 요구만 추가).
- 프런트 `fetch`는 같은 오리진이라 브라우저가 Origin을 자동으로 붙인다 — 변경 불필요.

## 3. 설정 탭 — rules.md 편집 (M6a)

**API**
- `GET /api/rules` → `{"text": str, "path": str, "sections": [str]}`. 파일이 없으면 `text`는 템플릿(첫 줄 `# 규칙 (rules.md)` H1 + `## 용어집`·`## 분류 규칙`·`## 요약 규칙`·`## 답변 규칙` 빈 섹션). `sections`는 **반환하는 `text`를 `load_rules_md`와 같은 파서로 파싱한 키 목록**(파일 유무와 무관하게 일관).
- `PUT /api/rules` `{"text": str}` → `text`가 str이 아니면 400; UTF-8 인코딩 바이트가 256KB 초과면 400(파일 크기 방어 목적 — 본문은 이미 파싱된 뒤라 메모리 보호는 아님). 원자적 저장(`.tmp` + `os.replace`). 응답 `{"ok": true, "sections": [...]}`.
- 저장 성공 후 `state["answerer"].update_rules(load_rules_md(path))` 호출.

**즉시 반영**
- `Answerer` Protocol에 `update_rules(sections: dict[str, str]) -> None` 추가. `ClaudeAnswerer`는 `answer_rules`·`glossary` 갱신, `FakeAnswerer`는 `self.rules = sections` 보관(테스트 관찰용). 커스텀 answerer는 코드베이스에 없음(전부 `FakeAnswerer`) — 호환 위험 없음.
- 동기화 경로는 이미 `run_sync`마다 `load_rules_md`를 다시 읽는다(app.py:146) — 변경 없음.
- **요약 규칙 주입 (상위 스펙 §9 표 준수)**: 현재 `## 요약 규칙`은 소비처가 없다(run_sync는 용어집·분류 규칙만 전달, `_SUMMARY_PROMPT`의 `rules` 자리에 분류 규칙이 들어감). `Summarizer.summarize_and_classify`에 `summary_rules: str = ""` 키워드 인자를 추가하고 `GeminiSummarizer._SUMMARY_PROMPT`에 `## 요약 규칙` 블록을 별도 주입, `FakeSummarizer`·`CountingSummarizer`(`*args, **kwargs` 위임이라 무변경)·`sync_local_docs`(`summary_rules` 인자 추가)·`run_sync`(`rules_md.get("요약 규칙", "")` 전달)까지 연결. GUI 템플릿의 4개 섹션이 전부 실제 소비처를 갖게 된다.

**notes 인덱싱 (§9 "rules.md는 notes로 취급되어 인덱싱됨")**
- `sync_notes(folders, excludes, state, extra_files: Sequence[Path] = ())` — 존재하는 `extra_files`를 폴더 스캔 결과와 합쳐 같은 파이프라인으로 처리. sid(절대경로) 기준 dedupe(`notes_folders`가 `data_dir`을 포함해 이미 수집된 경우 중복 임베딩 방지). `is_excluded`는 동일 적용. 제목은 `_title_of`(첫 `#` 줄) — 템플릿의 H1 `# 규칙 (rules.md)` 덕에 "용어집"이 제목이 되는 문제 없음.
- `run_sync("notes")`가 `extra_files=[cfg.rules_md_path]` 전달.

**UI** — 새 `설정` 탭: textarea(높이 60vh, 고정폭) + 저장 버튼 + 결과 한 줄(섹션 목록). **본문은 `.value`로, 경로·섹션 목록은 `textContent`로만 주입**(innerHTML 금지 — `</textarea><script>` XSS). 탭 진입 시 GET. 설정 탭에 §4 "전체 재요약"과 (M6b) "인덱스 재구축" 버튼을 둔다.

## 4. 재요약 (M6a)

**원리** — local_docs 상태 `files[sid]`를 **제거하지 않고 `[0.0, 0]`(`local_docs._RETRY_SENTINEL`과 같은 값)로 치환**한다. sid가 `prev`에 남으므로 (a) `run_sync`가 `prev["files"]` 키로 만드는 `prior_map`이 유지되어 `prior_category` 힌트와 `_place`의 `is_ours` 판정이 살아 **기존 요약 md를 덮어쓴다**(제거하면 `prior=None` → 해시 접미사 중복본 + 원본 복사본 재생성 + 분류 흔들림), (b) 실파일 시그니처와 결코 일치하지 않아 재요약이 강제되고, (c) 파일이 그 사이 삭제됐어도 `deleted` 판정이 정상 동작한다.

**API**
- `GET /api/resummarize/count` → `{"count": n}` — local_docs 상태 `files` 키 수.
- `POST /api/resummarize` `{"source_id": str}` 또는 `{"all": true}` (§2 보호 적용):
  1. `sync_lock` 안에서 상태를 읽어 대상 항목을 센티널로 치환 후 `set_sync_state`. 문서별인데 sid가 `files`에 없으면 404.
  2. 락 해제 후 `run_sync(state, "local_docs")` — 일일 상한 게이트·오류 격리·로그가 그대로 적용.
  3. 응답 = run_sync entry + `"reset": n`. `indexed == 0`이면 UI가 "0건 — 파일이 이미 없거나 상한/오류(로그 확인)"로 표시.
- 동기식(기존 `/api/sync`와 동일). 중복 실행 방지: `state["resummarizing"]` 플래그로 진행 중이면 409, UI 버튼 disable.

**UI**
- 채팅 출처 카드: `source_type == "local_docs"`에 "재요약" 버튼 → confirm → `POST {source_id: h.source_id}`(`url_or_path`가 아니라 `source_id` — `Hit`에 존재) → 결과 alert.
- 설정 탭 "전체 재요약": `GET count` → confirm(`n건을 다시 요약합니다. 요약 API를 최소 n회(비전 문서는 +1) 호출합니다.`) → `POST {all: true}`.

## 5. 사용량 GUI 표시 (M6a)

- `UsageTracker.recent_days(n) -> list[tuple[str, int]]` — 최근 n일(오늘 포함) `(날짜, 합계)` 오름차순, 기록 없는 날 제외, 락 안에서 계산.
- `GET /api/usage` → `{"today": {kind: n}, "total": n(오늘 합계), "limit": n, "indexing_allowed": bool, "days": [{"date", "total"}] (최근 7일)}`.
- UI: 소스 탭 표 위 `<div id="usageLine">` — 종류 순서 고정 `embed · summary · vision · answer`: "오늘 API 호출 12건 (embed 9 · summary 1 · vision 1 · answer 1) · 일일 상한 없음"; `limit>0`이면 "12 / 50건"; `indexing_allowed=false`면 "⚠️ 상한 도달 — 요약·인덱싱 일시정지 (검색·답변은 계속)". `loadSources()`가 함께 갱신. 값은 `textContent`.

## 6. rebuild — 인덱스 재구축 (M6b)

**의미** — 인덱스는 소모품, **요약 md·para_map·local_docs 동기화 상태·Atlassian 등록·usage.json·rules.md는 보존**. local_docs는 요약 md를 LLM 호출 없이 재사용한다.

**정상 경로 = 제자리 초기화 (파일 삭제 없음)** — `rebuild.reset_index(state) -> dict`:
0. 사전 검사(아무것도 바꾸기 전): `state["usage"].indexing_allowed()`가 False면 409 "일일 API 상한 도달 — 상한이 초기화된 뒤 실행하세요"(초기화 후 게이트에 막히면 빈 인덱스로 자정까지 고착); `cfg.watch_folders`·`cfg.notes_folders` 중 존재하지 않는 폴더가 있으면 409 + 폴더 목록(미마운트 드라이브 상태의 재구축 방지) — 단 요청 본문 `{"force": true}`면 경고를 무시하고 진행(UI는 409 응답의 폴더 목록을 confirm으로 보여준 뒤 force 재요청; 아래 미관측 sid 센티널 규칙 덕에 안전); `state["resummarizing"]` 또는 **인메모리** `state["rebuilding"]`이 True면 409(`meta` 마커로 판정하지 않는다 — 재개 대기 중엔 마커가 남아 있어 버튼이 영구 409가 됨).
1. `sync_lock` 안, 단일 트랜잭션: `SELECT id FROM documents`를 **`fetchall()`한 뒤** `indexer._delete_doc_rows`로 순회 삭제(스캔 중 삭제로 인한 행 건너뜀 방지)(fts5 external-content·vec0 삭제를 이미 정확히 처리하는 검증된 경로), `sync_state`에서 local_docs **제외** 전부 삭제, `meta`에 `rebuild_in_progress='1'` 기록. `para_map`·local_docs `sync_state`는 건드리지 않는다 → 스냅샷 파일 불필요. 커넥션 유지 → 클로저 캡처·WAL 삭제·경쟁 창 문제 없음(리더는 WAL 스냅샷 격리로 커밋 전/후 상태만 봄).
2. `state["force_reindex_local_docs"] = True`.
3. 응답 `{"ok": true, "phase": "resync", "targets": [...]}` — `ok`는 초기화 성공을 뜻한다. **재수집은 백그라운드 스레드**(`threading.Thread(daemon=True)`, `state["rebuilding"]=True`로 표시하고 `try/finally`로 반드시 해제)에서 `_scheduled_sources(state)` 순서로 `run_sync` 실행(등록 없는 소스 스킵 정책과 일관). 수천 문서·메일 1년치는 수십 분이 걸리므로 HTTP 요청 안에서 기다리지 않는다. 진행은 소스 탭 문서 수·로그 탭으로 관찰. **`meta.rebuild_in_progress` 마커는 local_docs `run_sync`가 `ok=True`로 `force_reindex` 플래그를 실제로 소비한 뒤에만 삭제한다** — 다른 소스의 오류(confluence 401 등)는 자체 `sync_state`가 비어 다음 라운드에 자가 복구되므로 마커와 무관하지만, local_docs는 복구 수단이 인메모리 플래그뿐이라 오류·상한 게이트로 소비되지 않은 채 마커를 지우면 재기동 후 영구 결손된다. 스케줄러의 local_docs 라운드가 백그라운드 스레드보다 먼저 플래그를 소비해도 무방(둘 다 `sync_lock` 직렬화, 나중 실행은 무동작 패스). outlook_mail은 `batch_size` 때문에 1패스로 복원이 끝나지 않고 `backlog`로 스케줄러 라운드에 이어진다 — 배너 문구에 명시.

**`sync_local_docs(..., force_reindex: bool = False)`**
- `force_reindex`이면 `prev[sid] == sig`인 파일도 건너뛰지 않는다. `prior_map[sid]`의 `summary_path`가 존재하고 읽히면(`read_text` 실패 = OSError/UnicodeDecodeError → 정상 요약 경로 폴백) 그 md 본문으로 `Document` 생성: `source_type="local_docs"`, `source_id=sid`, `title=path.name`, `text=본문`, `url_or_path=sid`, `updated_at=파일 mtime`, `content_indexed = DRM_MARKER not in 본문`, `extra={"para_path": prior[0], "summary_path": prior[1]}` — `summary_path`는 필수(run_sync의 `set_para_map` 조건과 `archive_project`의 접두사 치환이 이 키에 의존). `_place`는 호출하지 않는다(원본 복사본 재복사 불필요; 따라서 `para_overrides`도 재평가하지 않음 — 결정 기록). `seen[sid] = sig`.
- 요약 md가 없거나 시그니처가 바뀐 파일은 정상 요약 경로(LLM).
- **반환 state 규칙**: 이번 패스에서 관측하지 못한 `prev`의 sid(하위 폴더 미마운트·`stat()` 실패·잠김 등)는 **센티널 `[0.0, 0]`로 남긴다** — 반환 `state = {"files": {**{sid: [0.0, 0] for sid in prev if sid not in seen}, **seen}}`. `seen`만 저장하면 prior_map이 소실되어 파일 재등장 시 해시 접미사 중복 md가 생기고(C1 재발), `prev`를 그대로 합치면 documents는 비었는데 시그니처가 일치해 영구 스킵된다. 센티널이면 prior_map 유지 + 다음 동기화에서 재처리 + 실제 삭제 시 `deleted` 판정 정상.
- **삭제 판정을 수행하지 않는다** (`deleted=[]`, `_cleanup` 미호출) — 재구축은 복원이지 정리가 아니다. 감시 폴더 미마운트 시 전 문서가 deleted로 판정되어 요약 md가 unlink되는 사고 차단. 정리는 다음 정상 동기화가 담당.
- `DRM_MARKER = "🔒 DRM/암호화로 내용 미인덱싱"` 상수를 `local_docs`에 두고 생성·판정 양쪽이 사용.
- 플래그 해제 시점: `run_sync`는 `sync_local_docs`가 **정상 반환한 뒤에만** `force_reindex_local_docs`를 지운다(게이트·예외로 커넥터에 도달 못 하면 유지).
- 검증 지표: 재구축 전후 `usage.json`의 `summary`·`vision` 불변(재사용 경로는 summarizer를 호출하지 않음), `embed`만 증가.

**중단 재개** — 기동 시 `meta.rebuild_in_progress='1'`이면 `state["force_reindex_local_docs"]=True`. 배너는 기동 시뿐 아니라 **마커가 존재하는 동안 항상** 표시(`/api/sources`에 `rebuild_in_progress` 필드): 재수집 스레드가 도는 중이면 "재구축 진행 중 — 문서 수·로그 탭 참조 (메일은 백로그로 이어짐)", 스레드가 없으면 "이전 재구축이 완료되지 않았습니다 [재개]"(= 재수집 스레드 시작 버튼). 이 마커 덕에 재수집 도중 프로세스가 죽어도 "sync_state는 최신인데 documents는 빈" 고착이 생기지 않는다.

**스키마 불일치 경로 (M9의 임베딩 차원 변경 시 실제로 타는 경로)**
- `db.read_legacy_maps(path) -> (para_rows, local_docs_state)`: 버전 검사·확장 로드 없이 `sqlite3.connect`로 열어 `para_map` 전체와 `sync_state(local_docs)`만 읽고 닫는다(테이블 부재는 빈 결과). 이 두 테이블은 스키마 변경 대상이 아니다.
- `create_app`: `open_db`의 `SchemaMismatchError`를 잡아 `state["conn"]=state["read_conn"]=None`, `state["schema_mismatch"]=메시지`. 기동은 성공.
- 가드: `_require_db()` 헬퍼(없으면 503 `schema_mismatch` 메시지)를 DB를 만지는 모든 엔드포인트 진입부에 둔다 — `/api/para/projects`·`/api/open`·`/api/chat`·`/api/archive`·`/api/resummarize`·`/api/resummarize/count`·`/api/sync`. (이 가드와 아래 state 조회 리팩터는 **M6a 선행 태스크**로 먼저 들어간다.) `/api/sources`만 예외: `doc_count` 0 + `schema_mismatch` 필드로 정상 응답(배너 표시용). `run_sync`는 게이트 검사 **이전**에 `conn is None`이면 `entry.error`로 기록 후 반환(예외 금지). `scheduler_loop`의 `to_thread` 호출은 `try/except`+로그로 감싸 어떤 예외에도 루프가 죽지 않게 한다(기존 취약점 동시 해소). 엔드포인트·`search_fn`은 `state["read_conn"]`/`state["conn"]`을 호출 시점에 조회한다(현재 `read_conn` 클로저 캡처 7곳 + `/api/archive`의 `conn` 1곳; `run_sync`의 `conn` 획득도 락 내부로 이동).
- rebuild(스키마 불일치 상태, 전 구간 `sync_lock` 보유): `read_legacy_maps`로 매핑 확보 → `index.db`·`-wal`·`-shm` 삭제(열린 커넥션 없음) → `open_db` 2회 → `para_map`·local_docs `sync_state` 복원 → `rebuild_in_progress` 기록 → `state` 커넥션 교체, `schema_mismatch` 제거 → 백그라운드 재수집(정상 경로와 동일). `read_legacy_maps`마저 실패한 경우에만 "local_docs 전량 재요약(요약 API n회)" 경고를 배너·confirm에 표시.
- UI 배너: "index.db 스키마 불일치 — 재구축이 필요합니다 [재구축]". 재구축 confirm에는 대상 문서 수·예상 호출 수(embed 청크 수는 미상이므로 "문서 n건, 요약 재사용")를 표시.

**CLI** — `python -m llmsearch --config ... --rebuild [--yes]`: `create_app` 뒤 서버 기동 전에 `rebuild.reset_index` → 재수집을 **동기로** 실행(헤드리스)하고 소스별 결과를 로그 출력 후 정상 기동. `--yes`가 없으면 대상 문서 수·경고를 출력하고 확인 프롬프트. `rebuild.py`는 `web/app.py`를 import하지 않는다 — `SOURCES` 상수를 `llmsearch/sources.py`로 옮기고 `run_sync`·`_scheduled_sources`는 인자로 주입.

## 7. 오류 처리 요약

| 상황 | 동작 |
|---|---|
| 비로컬 Origin/비JSON 상태 변경 요청 | 403/415, 부작용 없음 |
| rules PUT 본문 비정상/256KB 초과 | 400, 파일 미변경 |
| 재요약 sid 미존재 / 진행 중 중복 | 404 / 409 |
| 재요약·재수집 중 일일 상한 도달 | run_sync 게이트가 entry.error로 안내, 나머지 소스 계속. 재구축은 1회 실행으로 상한을 초과할 수 있음(게이트는 소스 진입 시점만 검사) — confirm에 명시 |
| rebuild 사전 검사 실패(상한 도달·폴더 미존재·진행 중) | 409 + 사유, DB 무변경 |
| 재수집 도중 프로세스 종료 | `rebuild_in_progress` 마커로 재기동 시 배너 [재개] |
| 스키마 불일치 | 기동 성공, 배너 + DB 엔드포인트 503, rebuild로 복구 |
| 스키마 불일치 + legacy 읽기 실패 | 전량 재요약 경고 후 진행 |

## 8. 테스트

**M6a 단위** (Fake 주입, `TestClient(base_url="http://127.0.0.1")`): 로컬 오리진 의존성(외부 Origin 403, 비JSON 415, 정상 통과); rules GET 템플릿·sections 일관성, PUT 저장·원자성·400(비str/256KB)·`FakeAnswerer.rules` 갱신; 요약 규칙이 `GeminiSummarizer` 프롬프트에 주입됨(프롬프트 조립 함수 단위)·`sync_local_docs`→summarizer로 전달; notes `extra_files` 인덱싱·dedupe·제외 적용; resummarize 문서별(센티널 치환·prior 유지·요약 md 경로 불변·usage summary +1)·전체·404·409·삭제된 파일의 `deleted` 판정 유지; `recent_days`·`/api/usage`.

**M6b 단위**: `force_reindex` 요약 md 재사용(summary/vision 불변, 문서 수 복원, `extra.summary_path` 존재, DRM 문서 `content_indexed=False`, md 읽기 실패 폴백, 삭제 판정 없음, 미마운트 폴더 409); `reset_index` 트랜잭션(documents 0, para_map 유지, local_docs 상태 유지, 다른 sync_state 삭제, 마커 기록)·상한 도달 409·플래그 해제 시점; 재개 마커 기동; `read_legacy_maps`; 스키마 불일치 기동(meta 버전 강제 변경 DB) → `/api/sources` 필드·503 가드·run_sync entry.error·rebuild 복구; scheduler 예외 격리; CLI `--rebuild --yes` 헤드리스.

**E2E** (`tools/e2e/verify.py`, 기존 45건 무변경): 신규 시나리오는 **9단계(사용량 카운터)와 10단계(상한 소진 루프) 사이**에 삽입한다 — 10단계 이후엔 게이트에 막혀 성립하지 않는다. 예산: 기존 ≈12건 + 재요약(summary 1·vision 1·embed 1) + 재구축(embed ≈8) ≈ 24건 < 50, `MAX_ROUNDS=40` 충분 — 데모 상한 상향 불필요.
- M6a: 설정 탭 로드→편집→저장→재로드 일치(H1 템플릿 확인) / 소스 탭 사용량 한 줄 표시 / 출처 카드 "재요약" → usage `summary +1`·`vision +1`(데모 pptx는 비전 경로) / 설정 탭 "전체 재요약" confirm 문구에 건수.
- M6b: "인덱스 재구축" → 사전 검사 통과 → 재수집 완료 대기(문서 수 폴링) → **`notes`·`local_docs`·`outlook_mail`·`outlook_cal` 문서 수 복원**(confluence/jira는 등록 상태에 종속이라 제외) + 직전·직후 usage 스냅샷에서 `summary`·`vision` 불변.

## 9. 데이터 폴더·스키마 변경

- `rules.md` — 설정 탭 진입점 추가(파일 신규 아님)
- `meta.rebuild_in_progress` — 재구축 중에만 존재하는 마커 행 (스키마 버전 불변 — meta는 key/value)
- 스냅샷 파일 없음(초안의 `rebuild_snapshot.json` 폐기)

## 10. 결정 기록

| 결정 | 근거 |
|---|---|
| 재요약은 상태 항목 제거가 아닌 센티널 치환 | 제거하면 prior_map 소실 → 요약 md 중복 생성·분류 흔들림·삭제 감지 상실 (리뷰 C1) |
| rebuild는 파일 삭제가 아닌 제자리 행 삭제 | 정상 경로에서 경쟁 창·WAL 삭제 실패·스냅샷 파일이 소멸 (리뷰 I2). 커넥션 state 조회 리팩터는 스키마 불일치 경로(M9가 실제로 타는 경로)에 여전히 필요하므로 M6a 선행 태스크로 수행 |
| force_reindex는 삭제 판정 생략 | 미마운트 폴더 상태의 재구축이 요약 md를 지우는 사고 차단 (리뷰 C2) |
| 스키마 불일치는 legacy 읽기로 매핑 회수 | M9 차원 변경이 이 경로를 타므로 전량 재요약은 상위 스펙 §3 위반 (리뷰 C3) |
| 재수집은 백그라운드, 재요약은 동기 | 재구축은 수 시간 가능, 재요약 1건은 짧다 (리뷰 I10) |
| 상한 도달 상태 rebuild 거부 | 초기화 후 게이트에 막히면 빈 인덱스로 자정까지 고착 (리뷰 C4) |
| 요약 규칙 주입을 M6a에 포함 | GUI가 소비처 없는 섹션을 노출하면 안 됨 — 상위 스펙 §9 표 준수 (리뷰 I8) |
| 상태 변경 API 로컬 오리진 검사 | CSRF로 rebuild/전체 재요약 원격 트리거 차단 (리뷰 I9) |
| 재사용 경로는 `_place`·`para_overrides` 미적용 | 파일 재복사 불필요; 오버라이드 변경 반영은 재요약 버튼의 역할 |
| M6a/M6b 분할 | 실 태스크 12개, 위험 집중 부분의 리뷰 밀도 확보 (리뷰 I11) |
