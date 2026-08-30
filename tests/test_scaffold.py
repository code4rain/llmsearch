def test_package_importable():
    import llmsearch
    assert llmsearch.__version__ == "0.1.0"


def test_console_script_registered():
    import tomllib
    from pathlib import Path
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["llmsearch"] == "llmsearch.cli:main"
