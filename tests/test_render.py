from pathlib import Path

from llmsearch.render import MAX_SLIDES, FakeSlideRenderer


def test_fake_renderer_returns_registered_images():
    fake = FakeSlideRenderer(images={"deck.pptx": [b"png1", b"png2"]})
    out = fake.render(Path("/any/deck.pptx"))
    assert out == [b"png1", b"png2"]
    assert fake.calls == ["/any/deck.pptx"]


def test_fake_renderer_caps_at_max_slides():
    fake = FakeSlideRenderer(images={"big.pptx": [b"x"] * (MAX_SLIDES + 5)})
    assert len(fake.render(Path("big.pptx"))) == MAX_SLIDES


def test_fake_renderer_unknown_file_returns_empty():
    assert FakeSlideRenderer().render(Path("none.pptx")) == []
