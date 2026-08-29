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


def test_atlassian_config(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "data_dir: /d\natlassian:\n  confluence_base_url: https://wiki.corp.com\n"
        "  jira_base_url: https://jira.corp.com\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.confluence_base_url == "https://wiki.corp.com"
    assert cfg.jira_base_url == "https://jira.corp.com"
    # 미설정 시 빈 문자열
    cfg_file.write_text("data_dir: /d\n", encoding="utf-8")
    assert load_config(cfg_file).confluence_base_url == ""


def test_outlook_config_defaults_and_load(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "data_dir: /d\noutlook:\n  mail_folders: [\"inbox\"]\n  mail_since_days: 30\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.mail_folders == ["inbox"]
    assert cfg.mail_since_days == 30
    assert cfg.mail_batch_size == 200      # 기본값
    assert cfg.cal_past_days == 90 and cfg.cal_future_days == 180


def test_daily_api_call_limit_loaded(tmp_path):
    from llmsearch.config import load_config

    p = tmp_path / "config.yaml"
    p.write_text("data_dir: /tmp/x\nlimits:\n  daily_api_calls: 500\n", encoding="utf-8")
    assert load_config(p).daily_api_call_limit == 500


def test_daily_api_call_limit_default_zero(tmp_path):
    from llmsearch.config import load_config

    p = tmp_path / "config.yaml"
    p.write_text("data_dir: /tmp/x\n", encoding="utf-8")
    assert load_config(p).daily_api_call_limit == 0


def test_limits_key_present_but_empty_loads_zero(tmp_path):
    """`limits:` 키만 있고 값이 없으면 YAML은 None을 준다 — AttributeError 없이 0으로 로드."""
    from llmsearch.config import load_config

    p = tmp_path / "config.yaml"
    p.write_text("data_dir: /tmp/x\nlimits:\n", encoding="utf-8")
    assert load_config(p).daily_api_call_limit == 0


def test_export_to_notes_loaded_and_default(tmp_path):
    from llmsearch.config import Config, load_config

    p = tmp_path / "config.yaml"
    p.write_text("data_dir: /tmp/x\nchat:\n  export_to_notes: true\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.export_to_notes is True and cfg.exports_dir == cfg.data_dir / "exports"
    p.write_text("data_dir: /tmp/x\nchat:\n", encoding="utf-8")
    assert load_config(p).export_to_notes is False
    assert Config(data_dir=tmp_path).export_to_notes is False
