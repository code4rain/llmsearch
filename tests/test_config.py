from pathlib import Path
from llmsearch.config import load_config


def test_load_config(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
data_dir: /tmp/llmsearch-data
watch_folders: ["/docs/work"]
notes_folders: ["/notes"]
para:
  projects: ["프로젝트A"]
  areas: ["팀운영"]
rules:
  para_overrides:
    - match: "path:**/경영회의/**"
      target: "Areas/경영지원"
  exclude: ["folder:인사평가"]
sync_interval_minutes: 15
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.data_dir == Path("/tmp/llmsearch-data")
    assert cfg.watch_folders == [Path("/docs/work")]
    assert cfg.projects == ["프로젝트A"]
    assert cfg.areas == ["팀운영"]
    assert cfg.para_overrides[0]["target"] == "Areas/경영지원"
    assert cfg.exclude == ["folder:인사평가"]
    assert cfg.sync_interval_minutes == 15
    assert cfg.answer_model == "claude-opus-5"  # 기본값


def test_load_config_defaults(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("data_dir: /d\n", encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.sync_interval_minutes == 30
    assert cfg.watch_folders == []
    assert cfg.para_overrides == []
