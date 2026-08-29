# llmsearch M6 — 운영 완성 설계 (스펙 잔여 P1)

> 상위 스펙: `2026-08-17-llmsearch-design.md` §8 §9 §10. 로드맵: `2026-08-29-llmsearch-roadmap-m6-m9.md`.
> 목표: 상위 스펙에 명시됐지만 미구현인 운영 기능 4개를 채운다 — rules.md GUI 편집, 재요약 버튼, rebuild, 사용량 GUI 표시. 새 외부 의존성 없음.

## 1. 범위

| 기능 | 근거 | 산출물 |
|---|---|---|
| 설정 탭 — rules.md 편집 | §9 "GUI 설정 탭에서 편집하는 마크다운 파일 하나" | `GET/PUT /api/rules`, 설정 탭 UI, 답변기 규칙 즉시 반영, rules.md notes 인덱싱 |
| 재요약 (문서별/전체) | §9 "문서별/전체 '재요약' 수동 버튼" | `POST /api/resummarize`, `GET /api/resummarize/count`, 출처 카드·설정 탭 버튼 |
| rebuild | §8 "`rebuild` 명령으로 전체 재구축 — 기존 요약 md는 재사용", 스키마 불일치 안내 | `POST /api/rebuild`, CLI `--rebuild`, 스키마 불일치 배너, local_docs `force_reindex` |
| 사용량 GUI 표시 | §10 "GUI 표시는 추후" | `GET /api/usage`, 소스 탭 상단 표시, `UsageTracker.recent_days` |

범위 밖: 사용량 차트, rules.md 문법 검증, 재요약 진행률(동기식이라 불필요), 요약 모델 선택 UI, 부분 rebuild(소스별).

## 2. 설정 탭 — rules.md 편집

**API**
- `GET /api/rules` → `{"text": str, "path": str, "sections": [str]}` — 파일 없으면 `text`는 4개 섹션 헤더(`## 용어집`, `## 분류 규칙`, `## 요약 규칙`, `## 답변 규칙`)만 있는 템플릿, `sections`는 `load_rules_md` 결과의 키 목록.
- `PUT /api/rules` `{"text": str}` → 원자적 저장(tmp + `os.replace`, UTF-8). 응답 `{"ok": true, "sections": [...]}`. 본문이 str이 아니면 400. 크기 상한 256KB(400).

**즉시 반영**
- 동기화 경로는 이미 `run_sync`마다 `load_rules_md`를 다시 읽는다 — 변경 없음.
- 답변기: `Answerer` Protocol에 `update_rules(sections: dict[str, str]) -> None` 추가. `ClaudeAnswerer`는 `answer_rules`/`glossary`를 갱신, `FakeAnswerer`는 마지막 값을 `self.rules`에 보관(테스트 관찰용). `PUT` 성공 후 `state["answerer"].update_rules(load_rules_md(path))` 호출. 재시작 불필요.
- `create_app`의 answerer 기본 생성 시 rules.md 읽는 기존 코드는 유지 (기동 시 초기값).

**notes 인덱싱 (§9 "rules.md는 notes로 취급되어 인덱싱됨")**
- `sync_notes(folders, excludes, state, extra_files: list[Path] = ())` — `extra_files` 중 존재하는 파일을 폴더 스캔 결과에 합쳐 같은 파이프라인으로 처리(source_id = 절대경로, 제목 = 첫 `#` 헤더 또는 파일명). `run_sync("notes")`가 `extra_files=[cfg.rules_md_path]`를 넘긴다. 파일이 없으면 무시.

**UI** — 새 `설정` 탭: textarea(높이 60vh, 고정폭 글꼴) + "저장" 버튼 + 저장 결과 한 줄(감지된 섹션 목록). 탭 진입 시 `GET`으로 로드. 설정 탭에 §3의 "전체 재요약"·§4의 "인덱스 재구축" 버튼도 둔다 (운영 조작 집약).

## 3. 재요약

**원리** — local_docs 동기화 상태 `files[sid] = [mtime, size]`에서 항목을 제거하면 다음 동기화가 그 파일을 변경된 것으로 보고 다시 요약한다. `para_map`은 유지하므로 `prior_category` 힌트가 살아 분류가 안정적이고, `_place`가 기존 요약 md를 덮어쓴다.

**API**
- `GET /api/resummarize/count` → `{"count": n}` — local_docs 상태의 파일 수 (전체 재요약 confirm용).
- `POST /api/resummarize` `{"source_id": str}` 또는 `{"all": true}`:
  1. `sync_lock` 안에서 local_docs 상태를 읽어 대상 항목 제거 후 저장. 문서별인데 `source_id`가 상태에 없으면 404.
  2. 락 해제 후 `run_sync(state, "local_docs")` 호출(내부에서 다시 락) — 일일 상한 게이트·오류 격리·로그 기록이 그대로 적용된다.
  3. 응답 = run_sync entry에 `"reset": n` 추가.
- 동기식(기존 `/api/sync`와 동일). 전체 재요약은 파일 수만큼 요약 API를 소모하므로 UI가 건수와 함께 confirm한다.

**UI**
- 채팅 출처 카드: `source_type == "local_docs"`인 카드에 "재요약" 버튼 → confirm → `POST {source_id: url_or_path}` → 결과 alert(indexed 건수 또는 error).
- 설정 탭: "전체 재요약" 버튼 → `GET count` → confirm(`n건을 다시 요약합니다. 요약 API를 n회 호출합니다.`) → `POST {all: true}`.

## 4. rebuild — 인덱스 재구축

**의미** — 인덱스는 소모품(상위 스펙 §3), 요약 md·Atlassian 등록·usage.json·rules.md는 비용 산출물/설정이라 보존. **local_docs는 요약 md를 LLM 호출 없이 재사용**해 재인덱싱한다.

**절차** (`POST /api/rebuild`, `sync_lock` 보유 + `state["rebuilding"] = True`):
1. 스냅샷: local_docs `sync_state`와 `para_map` 전체를 `data_dir/rebuild_snapshot.json`에 기록 (스키마 불일치로 DB를 못 여는 경우엔 스냅샷 단계 생략 — 기존 스냅샷 파일이 있으면 그것을 사용).
2. `state["conn"]`·`state["read_conn"]` close → `index.db`, `index.db-wal`, `index.db-shm` 삭제 → `db.open_db` 2회로 재오픈 → `state`에 교체. **엔드포인트·search_fn은 클로저 변수가 아니라 `state["read_conn"]`/`state["conn"]`을 호출 시점에 조회하도록 수정한다** (현재 `read_conn` 클로저 캡처가 6곳).
3. 복원: 스냅샷의 `para_map` 행 삽입, local_docs `sync_state` 복원. `state["force_reindex_local_docs"] = True`.
4. 락 해제 후 전 소스(`SOURCES` 순서)에 대해 `run_sync` 실행. local_docs는 이 1회에 한해 `force_reindex=True`로 호출되고 플래그를 지운다. 다른 소스는 `sync_state`가 비었으므로 전량 재수집(메일은 기존 롤링 윈도우 콜드스타트 구조로 재개 가능).
5. 응답 `{"ok": bool, "entries": [run_sync entry ...]}`. 스냅샷 파일은 성공 시 삭제, 실패 시 보존(재시도용).

**`sync_local_docs(..., force_reindex: bool = False)`** — `force_reindex`이면 `prev[sid] == sig`인 파일도 건너뛰지 않고, `prior_map[sid]`의 `summary_path`가 존재하면 그 md 본문으로 `Document`를 만든다(`para_path`=prior category, `content_indexed`는 md에 DRM 표식(`🔒 DRM`)이 없으면 True). 요약 md가 없거나 시그니처가 바뀐 파일은 정상 요약 경로(LLM 호출). 검증 지표: rebuild 전후 `usage.json`의 `summary`/`vision` 카운트가 불변이어야 한다(요약 재사용 증명), `embed`만 증가.

**동시성** — `state["rebuilding"]`이 True인 동안 `/api/chat`·`/api/sync/*`·`/api/resummarize`·`/api/archive`는 503 `{"detail": "인덱스 재구축 중입니다"}`. 개인용 단일 사용자 도구이므로 락 계층 추가 대신 플래그로 단순화. 스케줄러 루프는 `rebuilding`이면 그 라운드를 건너뛴다.

**스키마 불일치 기동** — 현재 `open_db`가 `SchemaMismatchError`를 던져 기동이 실패한다. 변경: `create_app`이 예외를 잡아 `state["conn"] = state["read_conn"] = None`, `state["schema_mismatch"] = 메시지`. 이 상태에서 `/api/sources`는 `doc_count` 0에 `schema_mismatch` 필드 포함, 동기화/채팅은 503(메시지 안내). UI는 상단 배너 "index.db 스키마 불일치 — 재구축이 필요합니다 [재구축]". rebuild 절차 1단계에서 DB를 못 열므로 스냅샷은 기존 파일이 있을 때만 사용(없으면 local_docs 전량 재요약 — 배너에 이 비용을 명시).

**CLI** — `python -m llmsearch --config ... --rebuild`: `create_app` 뒤 서버 기동 전에 rebuild 함수를 직접 호출하고 결과를 로그로 출력한 뒤 정상 기동. rebuild 로직은 `web/app.py`가 아니라 `src/llmsearch/rebuild.py`에 두고 웹·CLI가 공유한다: `rebuild_index(state, run_sync) -> dict`.

## 5. 사용량 GUI 표시

- `UsageTracker.recent_days(n: int) -> list[tuple[str, int]]` — 최근 n일 `(날짜, 합계)`를 날짜 오름차순으로, 기록 없는 날은 제외 (락 안에서 계산).
- `GET /api/usage` → `{"today": {kind: n}, "total": n, "limit": n, "indexing_allowed": bool, "days": [{"date": "YYYY-MM-DD", "total": n}]}` (최근 7일).
- UI: 소스 탭 표 위에 한 줄 `<div id="usageLine">` — "오늘 API 호출 12건 (embed 9 · summary 1 · vision 1 · answer 1) · 일일 상한 없음". `limit > 0`이면 "12 / 50건", `indexing_allowed`가 false면 "⚠️ 상한 도달 — 요약·인덱싱 일시정지 (검색·답변은 계속)". `loadSources()`가 함께 갱신.

## 6. 오류 처리 요약

| 상황 | 동작 |
|---|---|
| rules PUT 본문 비정상/과대 | 400, 파일 미변경 |
| 재요약 source_id 미존재 | 404 |
| 재요약/rebuild 중 일일 상한 도달 | run_sync 게이트가 entry.error로 안내, 나머지 소스 계속 |
| rebuild 중 DB 삭제 실패(잠금 등) | 503 + 로그, `rebuilding` 해제, 스냅샷 보존, 기존 커넥션 재오픈 시도 |
| rebuild 중 다른 요청 | 503 "인덱스 재구축 중입니다" |
| 스키마 불일치 | 기동은 성공, 배너 + 503, rebuild로 복구 |

## 7. 테스트

- 단위(Fake 주입, TestClient `base_url="http://127.0.0.1"`): rules GET 템플릿/PUT 저장·섹션 파싱·크기 상한/답변기 `update_rules` 호출 확인; notes `extra_files`로 rules.md 인덱싱; resummarize 문서별·전체·404·상태 리셋 후 요약 재호출(usage `summary` 증가); `force_reindex` 요약 md 재사용(summary 카운트 불변, 문서 수 복원, DRM 표식 문서의 `content_indexed=False`); rebuild 스냅샷 생성·복원·`rebuilding` 중 503·성공 시 스냅샷 삭제; 스키마 불일치 기동(메타 버전을 강제로 바꾼 index.db) → 배너 필드·503·rebuild 복구; `recent_days`·`/api/usage`.
- E2E(`tools/e2e/verify.py` 확장, 기존 45건 무변경): 설정 탭 로드→편집→저장→재로드 일치; 사용량 한 줄 표시; 출처 카드 "재요약" → usage summary +1; 설정 탭 "전체 재요약" confirm 건수; "인덱스 재구축" → 소스 문서 수 복원 + usage summary/vision 불변. 데모 서버 일일 상한 50 예산 안에서 성립하도록 가드 유지(필요 시 데모 상한 상향).

## 8. 데이터 폴더 변경

```
llmsearch-data/
├─ rules.md                # 설정 탭에서 편집 (신규 아님 — GUI 진입점 추가)
├─ rebuild_snapshot.json   # rebuild 진행 중에만 존재 (성공 시 삭제)
└─ usage.json              # M5 — /api/usage가 읽음
```

## 9. 결정 기록

| 결정 | 근거 |
|---|---|
| 재요약·rebuild는 동기식 | 기존 수동 동기화와 동일한 모델, 진행률 UI 불필요, 개인용 단일 사용자 |
| rebuild 중 503 플래그 | 커넥션 교체를 안전하게 하는 최소 장치 — 락 계층 추가 대비 단순 |
| 요약 md 재사용은 `force_reindex` 1회 플래그 | 상시 "md 존재 시 재사용" 경로는 파일 변경 감지와 충돌 — rebuild 직후에만 필요 |
| rebuild 로직을 `rebuild.py`로 분리 | 웹·CLI 공유, `web/app.py` 비대화 방지 |
| 스키마 불일치를 기동 실패가 아닌 배너로 | M9 임베딩 차원 변경 시 GUI에서 복구 가능해야 함 |
