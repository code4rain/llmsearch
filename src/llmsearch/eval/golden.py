from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .. import db, search
from ..config import load_config


def _matches(expected: str, sid: str) -> bool:
	if expected == sid:
		return True
	norm = sid.replace("\\", "/")
	return norm.endswith("/" + expected.replace("\\", "/"))


GOLDEN_MAX_CASES = 50  # 1클릭 임베딩 예산 상한 (스펙 M7 §4)


def parse_golden(text: str) -> list[dict]:
	"""golden.yaml 본문 → 케이스 목록. CLI·API 공용. 위반은 ValueError(한국어 사유)."""
	try:
		data = yaml.safe_load(text)
	except yaml.YAMLError as exc:
		raise ValueError(f"YAML 파싱 실패: {exc}") from exc
	if data is None:
		return []
	if not isinstance(data, list):
		raise ValueError("golden.yaml은 목록([- question: ..., expect_source_id: ...])이어야 합니다")
	if len(data) > GOLDEN_MAX_CASES:
		raise ValueError(f"케이스가 {GOLDEN_MAX_CASES}건을 초과합니다 ({len(data)}건)")
	cases = []
	for i, item in enumerate(data, start=1):
		if not isinstance(item, dict):
			raise ValueError(f"{i}번째 항목이 객체가 아닙니다")
		q, e = item.get("question"), item.get("expect_source_id")
		if not isinstance(q, str) or not q.strip() or not isinstance(e, str) or not e.strip():
			raise ValueError(f"{i}번째 항목: question·expect_source_id는 비어 있지 않은 문자열이어야 합니다")
		cases.append({"question": q.strip(), "expect_source_id": e.strip()})
	return cases


def evaluate(conn, embedder, cases: list[dict]) -> dict:
	results = []
	for case in cases:
		found = [h.source_id for h in search.search(conn, embedder, case["question"], k=3)]
		rank = next((i + 1 for i, sid in enumerate(found) if _matches(case["expect_source_id"], sid)), None)
		results.append({"question": case["question"], "expected": case["expect_source_id"], "rank": rank, "got": found})
	total = len(cases)
	hits_at_3 = sum(1 for r in results if r["rank"] is not None)
	misses = [{"question": r["question"], "expected": r["expected"], "got": r["got"]} for r in results if r["rank"] is None]
	return {"total": total, "hit_at_3": hits_at_3,
			"rate": hits_at_3 / total if total else 0.0, "misses": misses, "cases": results}


def main():
	load_dotenv()
	parser = argparse.ArgumentParser()
	parser.add_argument("--config", type=Path, required=True)
	parser.add_argument("--golden", type=Path, default=None)
	args = parser.parse_args()
	cfg = load_config(args.config)
	golden_path = args.golden or (cfg.data_dir / "golden.yaml")
	if not golden_path.exists():
		print(f"golden.yaml이 없습니다: {golden_path}")
		sys.exit(1)
	try:
		cases = parse_golden(golden_path.read_text(encoding="utf-8"))
	except ValueError as exc:
		print(f"golden.yaml 오류: {exc}")
		sys.exit(1)
	if not cases:
		print("golden.yaml이 비어 있습니다")
		sys.exit(1)
	from ..embeddings import GeminiEmbeddings
	# GeminiEmbeddings를 직접 생성해 쓴다 — usage.py의 CountingEmbedder를 거치지 않으므로
	# 이 도구의 호출은 usage.json 카운팅·일일 API 상한 게이트 바깥에서 실제 API 예산을 소모한다.

	conn = db.open_db(cfg.db_path)
	report = evaluate(conn, GeminiEmbeddings(model=cfg.embed_model), cases)
	print(json.dumps(report, ensure_ascii=False, indent=2))
	target = 0.7  # 스펙 §1 성공 기준
	print(f"\n상위3 적중률 {report['rate']:.0%} (목표 {target:.0%}) -> {'PASS' if report['rate'] >= target else 'FAIL'}")


if __name__ == "__main__":
    main()
