from pathlib import Path

from llmsearch.atlassian.client import FakeAtlassianClient
from llmsearch.connectors.confluence import sync_confluence


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


def test_filesystem_unsafe_title_sanitized(tmp_path: Path):
    c = FakeAtlassianClient(pages={"7": page("7", "제목: 위험한*이름?")})
    r = sync_confluence(c, ["7"], {}, tmp_path)
    mirror = Path(r.documents[0].extra["mirror_path"])
    assert mirror.exists()
    for ch in ':*?"<>|':
        assert ch not in mirror.name
