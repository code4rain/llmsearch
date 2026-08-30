import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "llmsearch"
WRAPPER = SKILL / "scripts" / "llmsearch"


def test_skill_md_frontmatter():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: llmsearch\n")
    assert "description:" in text.split("---")[1]
    for rule in ("search", "get", "출처", "sync", "데이터"):
        assert rule in text


def test_wrapper_executable_and_uses_env_python(tmp_path: Path):
    assert os.access(WRAPPER, os.X_OK)
    fake_py = tmp_path / "py.sh"
    fake_py.write_text("#!/usr/bin/env bash\necho \"PY=$0 ARGS=$*\"\n", encoding="utf-8")
    fake_py.chmod(0o755)
    env = {**os.environ, "LLMSEARCH_PYTHON": str(fake_py)}
    out = subprocess.run([str(WRAPPER), "status", "--json"], env=env, capture_output=True, text=True, check=True).stdout
    assert out.strip() == f"PY={fake_py} ARGS=-m llmsearch.cli status --json"


def test_wrapper_reads_home_env_file(tmp_path: Path):
    fake_py = tmp_path / "py.sh"
    fake_py.write_text("#!/usr/bin/env bash\necho \"FROMFILE $*\"\n", encoding="utf-8")
    fake_py.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / "env").write_text(f"# comment\nLLMSEARCH_PYTHON={fake_py}\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "LLMSEARCH_PYTHON"}
    env["LLMSEARCH_HOME"] = str(home)
    out = subprocess.run([str(WRAPPER), "search", "q"], env=env, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "FROMFILE -m llmsearch.cli search q"


def test_wrapper_end_to_end_with_real_interpreter(tmp_path: Path):
    """실제 venv 인터프리터로 status를 호출 — 설정이 없으므로 exit 2와 안내가 나와야 한다."""
    env = {k: v for k, v in os.environ.items() if k not in ("LLMSEARCH_PYTHON", "LLMSEARCH_CONFIG")}
    env["LLMSEARCH_HOME"] = str(tmp_path / "nohome")
    env["LLMSEARCH_PYTHON"] = sys.executable
    r = subprocess.run([str(WRAPPER), "status"], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 2 and "install.sh" in r.stderr
