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

## 개발
- 테스트: `pytest` (WSL에서 실행 가능, API 키 불필요)
- 인덱스는 소모품: 스키마 변경·손상 시 `index.db` 삭제 후 재동기화
