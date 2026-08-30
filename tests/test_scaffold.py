def test_package_importable():
    import llmsearch
    assert llmsearch.__version__ == "0.1.0"


def test_console_script_registered():
    import tomllib
    from pathlib import Path
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["llmsearch"] == "llmsearch.cli:main"


def test_cli_import_does_not_pull_in_fastapi():
    """CLI는 얇은 어댑터 — import만으로 FastAPI를 끌어오면 안 된다 (SOURCES는 leaf 모듈에)."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    exe = root / ".venv" / "bin" / "python"
    proc = subprocess.run(
        [str(exe if exe.exists() else sys.executable), "-c",
         "import llmsearch.cli, sys; sys.exit(1 if 'fastapi' in sys.modules else 0)"],
        cwd=root, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"fastapi가 import됨\n{proc.stdout}\n{proc.stderr}"


def test_sources_defined_in_models_and_reexported_by_web_app():
    from llmsearch.models import SOURCES
    from llmsearch.web.app import SOURCES as WEB_SOURCES
    assert SOURCES == ("notes", "local_docs", "outlook_mail", "outlook_cal", "confluence", "jira")
    assert SOURCES is WEB_SOURCES
