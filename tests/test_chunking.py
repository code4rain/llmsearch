from llmsearch.chunking import chunk_text


def test_short_text_single_chunk():
    assert chunk_text("짧은 글") == ["짧은 글"]


def test_splits_on_paragraphs():
    text = "\n\n".join(f"문단{i} " + "가" * 300 for i in range(5))
    chunks = chunk_text(text, max_chars=800)
    assert all(len(c) <= 800 for c in chunks)
    assert len(chunks) >= 2
    assert chunks[0].startswith("문단0")


def test_long_paragraph_hard_split():
    chunks = chunk_text("가" * 2000, max_chars=800)
    assert all(len(c) <= 800 for c in chunks)
    assert "".join(chunks) == "가" * 2000


def test_empty():
    assert chunk_text("") == []
