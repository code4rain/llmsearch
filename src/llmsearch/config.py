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
    )
