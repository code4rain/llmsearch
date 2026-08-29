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


def evaluate(conn, embedder, cases: list[dict]) -> dict:
	hits_at_3 = 0
	misses = []
	for case in cases:
		results = search.search(conn, embedder, case["question"], k=3)
		found = [h.source_id for h in results]
		if any(_matches(case["expect_source_id"], sid) for sid in found):
			hits_at_3 += 1
		else:
			misses.append({"question": case["question"], "expected": case["expect_source_id"], "got": found})
	total = len(cases)
	return {"total": total, "hit_at_3": hits_at_3,
			"rate": hits_at_3 / total if total else 0.0, "misses": misses}


def main():
	load_dotenv()
	parser = argparse.ArgumentParser()
	parser.add_argument("--config", type=Path, required=True)
	parser.add_argument("--golden", type=Path, required=True)
	args = parser.parse_args()
	cfg = load_config(args.config)
	from ..embeddings import GeminiEmbeddings
	# GeminiEmbeddings를 직접 생성해 쓴다 — usage.py의 CountingEmbedder를 거치지 않으므로
	# 이 도구의 호출은 usage.json 카운팅·일일 API 상한 게이트 바깥에서 실제 API 예산을 소모한다.

	conn = db.open_db(cfg.db_path)
	cases = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
	cases = cases or []
	if not cases:
		print("golden.yaml이 비어 있습니다")
		sys.exit(1)
	report = evaluate(conn, GeminiEmbeddings(model=cfg.embed_model), cases)
	print(json.dumps(report, ensure_ascii=False, indent=2))
	target = 0.7  # 스펙 §1 성공 기준
	print(f"\n상위3 적중률 {report['rate']:.0%} (목표 {target:.0%}) -> {'PASS' if report['rate'] >= target else 'FAIL'}")


if __name__ == "__main__":
    main()
