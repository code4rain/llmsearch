from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import find_dotenv, load_dotenv


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
    export_to_notes: bool = False  # 내보낸 대화(md)를 notes로 인덱싱 (스펙 M8 §3)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def summaries_dir(self) -> Path:
        return self.data_dir / "summaries"

    @property
    def rules_md_path(self) -> Path:
        return self.data_dir / "rules.md"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    para = raw.get("para") or {}
    rules = raw.get("rules") or {}
    outlook = raw.get("outlook") or {}
    atlassian = raw.get("atlassian") or {}
    limits = raw.get("limits") or {}
    chat = raw.get("chat") or {}
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
        export_to_notes=bool(chat.get("export_to_notes", False)),
    )


class ConfigNotFound(FileNotFoundError):
    """설정 파일이 없음 — 메시지에 찾은 경로와 설치 안내를 담는다 (스킬 스펙 §3)."""


def llmsearch_home() -> Path:
    """전역 기준 디렉터리 — `$LLMSEARCH_HOME` 또는 `~/.llmsearch`."""
    return Path(os.environ.get("LLMSEARCH_HOME") or (Path.home() / ".llmsearch"))


def resolve_config_path(explicit: Path | None = None) -> Path:
    """설정 경로 결정: 인자 > $LLMSEARCH_CONFIG > $LLMSEARCH_HOME/config.yaml.

    '지정된 첫 후보'를 쓴다 — 존재하는 것을 찾아 내려가지 않는다(어느 설정이 읽혔는지 항상 결정적).
    """
    if explicit is not None:
        path = Path(explicit)
    elif os.environ.get("LLMSEARCH_CONFIG"):
        path = Path(os.environ["LLMSEARCH_CONFIG"])
    else:
        path = llmsearch_home() / "config.yaml"
    if not path.exists():
        raise ConfigNotFound(
            f"설정 파일이 없습니다: {path}\n"
            "  --config PATH 또는 LLMSEARCH_CONFIG로 지정하거나, "
            "skills/llmsearch/scripts/install.sh 로 ~/.llmsearch/config.yaml을 만드세요."
        )
    return path


def load_env() -> None:
    """`.env` 로드: 실제 환경변수 > cwd(상위 포함) .env > $LLMSEARCH_HOME/.env (override=False)."""
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found)
    load_dotenv(llmsearch_home() / ".env")
