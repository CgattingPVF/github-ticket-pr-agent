from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data")).resolve()
    workspace_root: Path = Path(os.getenv("WORKSPACE_ROOT", "./workspaces")).resolve()
    host: str = os.getenv("APP_HOST", "127.0.0.1")
    port: int = int(os.getenv("APP_PORT", "3060"))
    secret_key: str = os.getenv("SECRET_KEY", "development-only-change-me")
    agent_command: str = os.getenv(
        "AGENT_COMMAND",
        "codex exec --sandbox workspace-write --ask-for-approval never -",
    )
    review_command: str = os.getenv(
        "REVIEW_COMMAND",
        "codex exec --sandbox workspace-write --ask-for-approval never -",
    )
    claude_command: str = os.getenv(
        "CLAUDE_COMMAND",
        "claude -p --output-format stream-json --verbose --dangerously-skip-permissions",
    )
    command_timeout_seconds: int = int(os.getenv("COMMAND_TIMEOUT_SECONDS", "3600"))
    review_timeout_seconds: int = int(os.getenv("REVIEW_TIMEOUT_SECONDS", "120"))
    minimum_confidence: float = float(os.getenv("MINIMUM_CONFIDENCE", "0.90"))
    max_repair_cycles: int = int(os.getenv("MAX_REPAIR_CYCLES", "0"))
    max_gate_attempts: int = int(os.getenv("MAX_GATE_ATTEMPTS", "6"))
    close_issue_on_merge: bool = _bool("CLOSE_ISSUE_ON_MERGE", False)
    comment_on_failure: bool = _bool("COMMENT_ON_FAILURE", False)
    editor_command: str = os.getenv("EDITOR_COMMAND", "code --reuse-window")
    local_repo_path: Path | None = Path(os.getenv("LOCAL_REPO_PATH")).resolve() if os.getenv("LOCAL_REPO_PATH") else None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
