from pathlib import Path

from llmsearch.atlassian.client import FakeAtlassianClient
from llmsearch.connectors import confluence
from llmsearch.connectors.confluence import _mirror_path, sync_confluence


def page(pid, title, version=1, ancestors=None, html="<p>본문</p>"):
    return {"id": pid, "space": "ENG", "title": title, "html": html,
            "version": version, "updated": "2026-08-01T10:00:00",
            "ancestors": ancestors or [], "url": f"https://wiki/pages/{pid}"}


def make_client():
    return FakeAtlassianClient(
        pages={"1": page("1", "루트"), "2": page("2", "자식", ancestors=["루트"]),
               "3": page("3", "손자", ancestors=["루트", "자식"])},
        children={"1": ["2"], "2": ["3"]},
    )


def test_tree_sync_and_mirror(tmp_path: Path):
    r = sync_confluence(make_client(), ["1"], {}, tmp_path)
    assert {d.source_id for d in r.documents} == {"1", "2", "3"}
    d3 = next(d for d in r.documents if d.source_id == "3")
    mirror = Path(d3.extra["mirror_path"])
    assert mirror.exists()
    assert mirror.parent.name == "자식" and "손자__3" in mirror.name  # 조상 경로 + id 접미사
    assert d3.source_type == "confluence" and "본문" in d3.text


def test_unchanged_not_reemitted(tmp_path: Path):
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    r2 = sync_confluence(c, ["1"], r1.state, tmp_path)
    assert r2.documents == [] and r2.deleted_ids == []


def test_version_bump_reemitted(tmp_path: Path):
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    c.pages["2"] = page("2", "자식", version=2, ancestors=["루트"], html="<p>수정됨</p>")
    r2 = sync_confluence(c, ["1"], r1.state, tmp_path)
    assert [d.source_id for d in r2.documents] == ["2"]
    assert "수정됨" in Path(r2.documents[0].extra["mirror_path"]).read_text(encoding="utf-8")


def test_removed_page_deleted_with_mirror(tmp_path: Path):
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    mirror3 = Path(next(d for d in r1.documents if d.source_id == "3").extra["mirror_path"])
    del c.pages["3"]; c.children["2"] = []
    r2 = sync_confluence(c, ["1"], r1.state, tmp_path)
    assert r2.deleted_ids == ["3"]
    assert not mirror3.exists()


def test_inaccessible_root_isolated(tmp_path: Path):
    c = make_client()
    r = sync_confluence(c, ["999", "1"], {}, tmp_path)  # 999는 KeyError
    assert {d.source_id for d in r.documents} == {"1", "2", "3"}  # 나머지 루트는 정상


def test_ancestor_dotdot_does_not_escape_mirror_dir(tmp_path: Path):
    """조상 제목이 ".."이어도 미러 경로는 mirror_dir 밖으로 탈출하지 않는다 (스펙 §7.2 P0 보안).

    _sanitize_segment가 ".."을 "_"로 막는 1계층 방어와, _mirror_path가 최종 경로를
    relative_to로 재검증하는 2계층 방어(defense in depth)를 함께 확인한다.
    """
    outside_marker = tmp_path.parent / "notes"
    c = FakeAtlassianClient(
        pages={"9": page("9", "문서", ancestors=["..", "..", "notes"])}
    )
    r = sync_confluence(c, ["9"], {}, tmp_path)
    mirror = Path(r.documents[0].extra["mirror_path"])
    assert mirror.exists()
    mirror.resolve().relative_to(tmp_path.resolve())  # ValueError면 탈출 — 테스트 실패
    assert not outside_marker.exists()


def test_mirror_path_layer2_falls_back_when_sanitize_bypassed(tmp_path: Path, monkeypatch):
    """1계층(_sanitize_segment)이 우회되더라도 _mirror_path의 relative_to 재검증이
    mirror_dir 밖 경로를 평면(flat) 폴백으로 막아야 한다 (defense in depth)."""
    monkeypatch.setattr(confluence, "_sanitize_segment", lambda s: s)  # 살균 우회 시뮬레이션
    p = page("9", "문서", ancestors=["..", "..", "notes"])
    mirror = _mirror_path(tmp_path, p)
    mirror.resolve().relative_to(tmp_path.resolve())  # ValueError면 탈출 — 테스트 실패
    assert mirror.parent == tmp_path  # flat fallback: mirror_dir 직하


def test_filesystem_unsafe_title_sanitized(tmp_path: Path):
    c = FakeAtlassianClient(pages={"7": page("7", "제목: 위험한*이름?")})
    r = sync_confluence(c, ["7"], {}, tmp_path)
    mirror = Path(r.documents[0].extra["mirror_path"])
    assert mirror.exists()
    for ch in ':*?"<>|':
        assert ch not in mirror.name


def test_transient_keyerror_not_deleted(tmp_path: Path):
    """접근 실패(KeyError)와 삭제를 구분: 자식 목록엔 남아있는데 조회만 실패하면
    미스 카운터만 올리고 이전 상태를 이월한다 — 삭제 처리하지 않는다."""
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    mirror3 = Path(next(d for d in r1.documents if d.source_id == "3").extra["mirror_path"])
    del c.pages["3"]  # children["2"] == ["3"]는 그대로 — fetch는 시도되고 KeyError

    r2 = sync_confluence(c, ["1"], r1.state, tmp_path)

    assert r2.deleted_ids == []
    assert mirror3.exists()
    assert "3" in r2.state["versions"]
    assert r2.state["misses"]["3"] == 1


def test_three_consecutive_misses_deletes(tmp_path: Path):
    """연속 3회 KeyError에 도달하면 라운드 안전 여부와 무관하게 진짜 삭제로 확정한다."""
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    mirror3 = Path(next(d for d in r1.documents if d.source_id == "3").extra["mirror_path"])
    del c.pages["3"]  # children["2"] == ["3"]는 유지 — 매 라운드 fetch 시도 후 KeyError

    r2 = sync_confluence(c, ["1"], r1.state, tmp_path)  # miss 1
    assert r2.state["misses"]["3"] == 1
    assert r2.deleted_ids == []
    assert mirror3.exists()

    r3 = sync_confluence(c, ["1"], r2.state, tmp_path)  # miss 2
    assert r3.state["misses"]["3"] == 2
    assert r3.deleted_ids == []
    assert mirror3.exists()

    r4 = sync_confluence(c, ["1"], r3.state, tmp_path)  # miss 3 — 진짜 삭제 확정
    assert r4.deleted_ids == ["3"]
    assert not mirror3.exists()


def test_cap_truncation_not_deleted(tmp_path: Path, monkeypatch):
    """MAX_PAGES_PER_TREE에 걸려 잘린 라운드(UNSAFE)에서는 순회 밖 페이지를
    삭제로 오판하지 않고 이전 상태를 이월한다."""
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    mirror3 = Path(next(d for d in r1.documents if d.source_id == "3").extra["mirror_path"])

    monkeypatch.setattr(confluence, "MAX_PAGES_PER_TREE", 2)
    r2 = sync_confluence(c, ["1"], r1.state, tmp_path)

    assert "3" not in {d.source_id for d in r2.documents}
    assert r2.deleted_ids == []
    assert mirror3.exists()
    assert "3" in r2.state["versions"]
    assert r2.state["versions"]["3"] == r1.state["versions"]["3"]


def test_empty_mirror_dir_pruned_on_delete(tmp_path: Path):
    """삭제된 페이지의 미러를 지운 뒤, 비어버린 상위(조상) 디렉터리도 정리된다."""
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    mirror3 = Path(next(d for d in r1.documents if d.source_id == "3").extra["mirror_path"])
    grandchild_dir = mirror3.parent  # .../ENG/루트/자식 — 손자__3.md만 담고 있던 디렉터리
    assert grandchild_dir.is_dir()

    del c.pages["3"]; c.children["2"] = []
    sync_confluence(c, ["1"], r1.state, tmp_path)

    assert not grandchild_dir.exists()
    assert grandchild_dir.parent.exists()  # .../ENG/루트 — 자식__2.md가 남아있어 유지됨


def test_unknown_root_keyerror_keeps_round_safe(tmp_path: Path):
    """처음 보는(미등록/오탈자) 루트의 KeyError는 지킬 이전 자식이 없으므로
    라운드를 UNSAFE로 만들지 않는다 — 같은 호출 안의 진짜 삭제는 정상 반영된다."""
    c = make_client()
    r1 = sync_confluence(c, ["1"], {}, tmp_path)
    mirror3 = Path(next(d for d in r1.documents if d.source_id == "3").extra["mirror_path"])
    del c.pages["3"]; c.children["2"] = []  # "3"은 진짜로 사라짐

    r2 = sync_confluence(c, ["999", "1"], r1.state, tmp_path)  # 999는 KeyError(미지의 pid)

    assert r2.deleted_ids == ["3"]
    assert not mirror3.exists()
