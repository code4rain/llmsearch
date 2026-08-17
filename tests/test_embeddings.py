import math

from llmsearch.embeddings import FakeEmbeddings, _l2_normalize


def test_l2_normalize_unit_norm():
    vec = [3.0, 4.0]  # 3-4-5 triangle
    normalized = _l2_normalize(vec)
    assert len(normalized) == 2
    norm = math.sqrt(sum(v * v for v in normalized))
    assert abs(norm - 1.0) < 1e-9  # unit norm


def test_l2_normalize_zero_vector():
    vec = [0.0, 0.0, 0.0]
    normalized = _l2_normalize(vec)
    assert normalized == [0.0, 0.0, 0.0]  # 0-vector stays 0


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
