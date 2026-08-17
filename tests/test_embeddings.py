from llmsearch.embeddings import FakeEmbeddings


def test_fake_embeddings_deterministic():
    e = FakeEmbeddings(dim=8)
    v1 = e.embed(["안녕", "하이"])
    v2 = e.embed(["안녕", "하이"])
    assert v1 == v2
    assert len(v1) == 2 and len(v1[0]) == 8


def test_fake_embeddings_similar_text_closer():
    e = FakeEmbeddings(dim=64)
    a, b, c = e.embed(["프로젝트A 회의록", "프로젝트A 회의 기록", "점심 메뉴"])
    def dist(x, y):
        return sum((i - j) ** 2 for i, j in zip(x, y))
    assert dist(a, b) < dist(a, c)  # 토큰 겹침이 많을수록 가깝다
