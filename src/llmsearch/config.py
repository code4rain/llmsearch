from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    data_dir: Path
    watch_folders: list[Path] = field(default_factory=list)
    notes_folders: list[Path] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    areas: list[str] = field(default_factory=list)
    para_overrides: list[dict] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    sync_interval_minutes: int = 30
    answer_model: str = "claude-opus-5"
    summary_model: str = "gemini-flash-latest"
    embed_model: str = "gemini-embedding-001"
    mail_folders: list[str] = field(default_factory=lambda: ["inbox", "sent"])
    mail_since_days: int = 365
    mail_batch_size: int = 200
    cal_past_days: int = 90
    cal_future_days: int = 180
    confluence_base_url: str = ""
    jira_base_url: str = ""
    daily_api_call_limit: int = 0  # 0 = 무제한 (스펙 §10 P2)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def summaries_dir(self) -> Path:
        return self.data_dir / "summaries"

    @property
    def rules_md_path(self) -> Path:
        return self.data_dir / "rules.md"


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    para = raw.get("para", {})
    rules = raw.get("rules", {})
    outlook = raw.get("outlook", {})
    atlassian = raw.get("atlassian", {})
    limits = raw.get("limits", {})
    return Config(
        data_dir=Path(raw["data_dir"]),
        watch_folders=[Path(p) for p in raw.get("watch_folders", [])],
        notes_folders=[Path(p) for p in raw.get("notes_folders", [])],
        projects=list(para.get("projects", [])),
        areas=list(para.get("areas", [])),
        para_overrides=list(rules.get("para_overrides", [])),
        exclude=list(rules.get("exclude", [])),
        sync_interval_minutes=int(raw.get("sync_interval_minutes", 30)),
        answer_model=raw.get("answer_model", "claude-opus-5"),
        summary_model=raw.get("summary_model", "gemini-flash-latest"),
        embed_model=raw.get("embed_model", "gemini-embedding-001"),
        mail_folders=list(outlook.get("mail_folders", ["inbox", "sent"])),
        mail_since_days=int(outlook.get("mail_since_days", 365)),
        mail_batch_size=int(outlook.get("mail_batch_size", 200)),
        cal_past_days=int(outlook.get("cal_past_days", 90)),
        cal_future_days=int(outlook.get("cal_future_days", 180)),
        confluence_base_url=str(atlassian.get("confluence_base_url", "")).rstrip("/"),
        jira_base_url=str(atlassian.get("jira_base_url", "")).rstrip("/"),
        daily_api_call_limit=int(limits.get("daily_api_calls", 0)),
    )
