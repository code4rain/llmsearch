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


def test_wrapper_reads_home_env_file_with_crlf(tmp_path: Path):
    """CRLF로 저장된 env 파일에서도 인터프리터 경로에 캐리지 리턴이 섞이지 않아야 한다."""
    fake_py = tmp_path / "py.sh"
    fake_py.write_text("#!/usr/bin/env bash\necho \"CRLF $*\"\n", encoding="utf-8")
    fake_py.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / "env").write_bytes(f"# comment\r\nLLMSEARCH_PYTHON={fake_py}\r\n".encode("utf-8"))
    env = {k: v for k, v in os.environ.items() if k != "LLMSEARCH_PYTHON"}
    env["LLMSEARCH_HOME"] = str(home)
    out = subprocess.run([str(WRAPPER), "status"], env=env, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "CRLF -m llmsearch.cli status"


def test_wrapper_requires_home_when_unset(tmp_path: Path):
    """LLMSEARCH_HOME도 HOME도 없으면 set -u로 죽는 대신 안내 메시지를 낸다."""
    env = {k: v for k, v in os.environ.items() if k not in ("HOME", "LLMSEARCH_HOME", "LLMSEARCH_PYTHON")}
    r = subprocess.run([str(WRAPPER), "status"], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "LLMSEARCH_HOME" in r.stderr


def test_wrapper_end_to_end_with_real_interpreter(tmp_path: Path):
    """실제 venv 인터프리터로 status를 호출 — 설정이 없으므로 exit 2와 안내가 나와야 한다."""
    env = {k: v for k, v in os.environ.items() if k not in ("LLMSEARCH_PYTHON", "LLMSEARCH_CONFIG")}
    env["LLMSEARCH_HOME"] = str(tmp_path / "nohome")
    env["LLMSEARCH_PYTHON"] = sys.executable
    r = subprocess.run([str(WRAPPER), "status"], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 2 and "install.sh" in r.stderr


INSTALL = SKILL / "scripts" / "install.sh"


def _install_env(tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("LLMSEARCH_PYTHON", "LLMSEARCH_CONFIG")}
    env["LLMSEARCH_HOME"] = str(tmp_path / "home")
    env["CLAUDE_SKILLS_DIR"] = str(tmp_path / "skills")
    return env


def test_install_creates_home_and_link_idempotent(tmp_path: Path):
    env = _install_env(tmp_path)
    for _ in range(2):  # 두 번 실행해도 같은 결과
        r = subprocess.run([str(INSTALL), "--python", sys.executable], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    home = tmp_path / "home"
    assert (home / "config.yaml").read_text(encoding="utf-8") == (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert (home / ".env").read_text(encoding="utf-8") == (ROOT / ".env.example").read_text(encoding="utf-8")
    assert (home / "env").read_text(encoding="utf-8") == f"LLMSEARCH_PYTHON={sys.executable}\n"
    link = tmp_path / "skills" / "llmsearch"
    assert link.is_symlink() and link.resolve() == SKILL.resolve()


def test_install_keeps_existing_config(tmp_path: Path):
    env = _install_env(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("data_dir: /keep\n", encoding="utf-8")
    subprocess.run([str(INSTALL), "--python", sys.executable], env=env, capture_output=True, text=True, check=True)
    assert (home / "config.yaml").read_text(encoding="utf-8") == "data_dir: /keep\n"


def test_install_refuses_real_directory_at_link(tmp_path: Path):
    env = _install_env(tmp_path)
    (tmp_path / "skills" / "llmsearch").mkdir(parents=True)
    r = subprocess.run([str(INSTALL), "--python", sys.executable], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "심볼릭 링크가 아닌" in r.stderr


def test_install_warns_missing_python(tmp_path: Path):
    env = _install_env(tmp_path)
    r = subprocess.run([str(INSTALL), "--python", str(tmp_path / "nope")], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "인터프리터" in r.stderr


def test_install_python_flag_requires_value(tmp_path: Path):
    env = _install_env(tmp_path)
    r = subprocess.run([str(INSTALL), "--python"], env=env, capture_output=True, text=True)
    assert r.returncode == 2 and "--python 뒤에 경로가 필요합니다" in r.stderr
