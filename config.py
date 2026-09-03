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
        "/home/claytongatting/.npm-global/bin/codex exec --sandbox workspace-write --ask-for-approval never -",
    )
    review_command: str = os.getenv(
        "REVIEW_COMMAND",
        "/home/claytongatting/.npm-global/bin/codex exec --sandbox workspace-write --ask-for-approval never -",
    )
    opencode_command: str = os.getenv(
        "OPENCODE_COMMAND",
        "opencode run --model opencode/big-pickle",
    )
    claude_command: str = os.getenv(
        "CLAUDE_COMMAND",
        "claude -p --model claude-haiku-4-5 --output-format stream-json --verbose --dangerously-skip-permissions",
    )
    command_timeout_seconds: int = int(os.getenv("COMMAND_TIMEOUT_SECONDS", "3600"))
    # Testing is an interactive evidence workflow. A silent provider should
    # escalate promptly instead of inheriting the hour-long delivery timeout.
    testing_pass_timeout_seconds: int = int(os.getenv("TESTING_PASS_TIMEOUT_SECONDS", "600"))
    testing_max_attempts: int = int(os.getenv("TESTING_MAX_ATTEMPTS", "3"))
    # A zero value disables the watchdog. Keep explicit environment overrides
    # supported, but bound the default so a stalled provider cannot leave a job
    # showing LIVE indefinitely.
    agent_pass_timeout_seconds: int = int(os.getenv("AGENT_PASS_TIMEOUT_SECONDS", "1800"))
    review_timeout_seconds: int = int(os.getenv("REVIEW_TIMEOUT_SECONDS", "900"))
    full_delivery_target_seconds: int = int(os.getenv("FULL_DELIVERY_TARGET_SECONDS", "180"))
    full_delivery_publish_reserve_seconds: int = int(os.getenv("FULL_DELIVERY_PUBLISH_RESERVE_SECONDS", "30"))
    minimum_confidence: float = float(os.getenv("MINIMUM_CONFIDENCE", "0.80"))
    max_repair_cycles: int = int(os.getenv("MAX_REPAIR_CYCLES", "1"))
    stage_stall_threshold_ms: int = int(os.getenv("STAGE_STALL_THRESHOLD_MS", "180000"))
    max_gate_attempts: int = int(os.getenv("MAX_GATE_ATTEMPTS", "15"))
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
