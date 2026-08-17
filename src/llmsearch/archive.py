"""프로젝트 완료(Archive) 워크플로 (스펙 §7.1 P1).

GUI에서 프로젝트 완료 처리 → `summaries/Projects/<name>/` 폴더를 `Archives/<name>/`로
이동하고, documents(para_path, extra_json)와 para_map을 새 경로로 갱신한다. 검색 랭킹의
Archives/ 감쇠(스펙 §8)는 para_path 프리픽스를 보므로 이 갱신만으로 즉시 적용된다.
원본 파일(watch 폴더)은 건드리지 않는다 — 다음 local_docs 동기화는 para_map의 사전
분류(prior)가 Archives/<name>이므로 재분류 없이 그대로 유지된다.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

# summarize의 내부 헬퍼를 의도적으로 재사용 — 프로젝트 이름이 파일시스템 세그먼트로
# 안전한지(경로 구분자·`..`·예약명 없음)를 분류 경로와 같은 규칙으로 판정하기 위해서다.
from .summarize import _sanitize_segment


def archive_project(conn: sqlite3.Connection, summaries_dir: Path, name: str) -> dict:
    if not name or _sanitize_segment(name) != name:
        raise ValueError(f"잘못된 프로젝트 이름입니다: {name!r}")
    src = summaries_dir / "Projects" / name
    dst = summaries_dir / "Archives" / name
    if not src.is_dir():
        raise KeyError(f"Projects/{name} 폴더가 없습니다")
    if dst.exists():
        raise ValueError(f"Archives/{name}가 이미 있습니다 — 기존 폴더를 정리한 뒤 다시 시도하세요")

    old_para, new_para = f"Projects/{name}", f"Archives/{name}"
    old_prefix, new_prefix = str(src), str(dst)

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    try:
        moved_docs = 0
        for doc_id, extra_json in conn.execute(
            "SELECT id, extra_json FROM documents WHERE para_path=?", (old_para,)
        ).fetchall():
            extra = json.loads(extra_json or "{}")
            extra["para_path"] = new_para
            sp = extra.get("summary_path")
            if isinstance(sp, str) and sp.startswith(old_prefix):
                extra["summary_path"] = new_prefix + sp[len(old_prefix):]
            conn.execute(
                "UPDATE documents SET para_path=?, extra_json=? WHERE id=?",
                (new_para, json.dumps(extra, ensure_ascii=False), doc_id),
            )
            moved_docs += 1

        moved_maps = 0
        for source_id, summary_path in conn.execute(
            "SELECT source_id, summary_path FROM para_map WHERE para_path=?", (old_para,)
        ).fetchall():
            new_summary = (
                new_prefix + summary_path[len(old_prefix):]
                if summary_path.startswith(old_prefix) else summary_path
            )
            conn.execute(
                "UPDATE para_map SET para_path=?, summary_path=? WHERE source_id=?",
                (new_para, new_summary, source_id),
            )
            moved_maps += 1
        conn.commit()
    except Exception:
        # DB 갱신 실패 시 폴더 이동을 되돌린다 — 파일과 인덱스가 서로 다른 위치를
        # 가리키는 반쪽 상태를 남기지 않기 위해서다. rollback은 실패해도 무시(이미 예외 전파 중).
        try:
            conn.rollback()
        except Exception:
            pass
        shutil.move(str(dst), str(src))
        raise

    return {
        "project": name, "documents": moved_docs, "mappings": moved_maps,
        "hint": (
            f"config.yaml의 para.projects에서 '{name}'을 제거하세요 — 활성 목록에 남아 있으면 "
            f"새 문서가 다시 Projects/{name}로 분류될 수 있습니다"
        ),
    }
