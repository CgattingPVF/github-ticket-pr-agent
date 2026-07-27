from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse


ISSUE_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)/?$")
SAFE_REPO_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# Linear escalation pipeline: Haiku 4.5 -> Sonnet 5 -> Luna -> Opus 4.7 ->
# Sol Thinking Low -> Sol Thinking High.
# "gpt-5.6-sol-high" is a synthetic key (same model as "gpt-5.6-sol", higher
# reasoning effort) that workflow.py's _escalate_command resolves specially.
MODEL_ESCALATION = {
    "claude-haiku-4-5": "claude-sonnet-5",
    "claude-sonnet-5": "gpt-5.6-luna",
    "gpt-5.6-luna": "claude-opus-4-7",
    "claude-opus-4-7": "gpt-5.6-sol",
    "gpt-5.6-sol": "gpt-5.6-sol-high",
}


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str


class WorkflowError(RuntimeError):
    pass


def parse_issue_url(url: str) -> IssueRef:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError("Enter a full https://github.com/.../issues/<number> URL.")
    match = ISSUE_PATH_RE.match(parsed.path)
    if not match:
        raise ValueError("The URL must point to a GitHub issue or pull request.")
    owner, repo, number = match.groups()
    if (
        not SAFE_REPO_PART_RE.fullmatch(owner)
        or not SAFE_REPO_PART_RE.fullmatch(repo)
        or owner.startswith("-")
        or repo.startswith("-")
    ):
        raise ValueError("The GitHub owner or repository name is invalid.")
    return IssueRef(owner=owner, repo=repo, number=int(number))


def validate_ref_name(value: str, field_name: str = "branch") -> str:
    value = value.strip()
    if not value or not SAFE_REF_RE.fullmatch(value) or value.startswith("-"):
        raise ValueError(f"Invalid {field_name} name.")
    if value.startswith("/") or value.endswith("/") or "//" in value or ".." in value:
        raise ValueError(f"Invalid {field_name} name.")
    return value


def slugify(value: str, max_length: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "ticket-fix")[:max_length].rstrip("-")


def make_branch_name(prefix: str, issue_number: int, title: str) -> str:
    prefix = validate_ref_name(prefix.strip().strip("/"), "branch prefix")
    branch = validate_ref_name(f"{prefix}/{issue_number}-{slugify(title)}")
    if len(branch) > 200:
        raise ValueError("Generated branch name is too long.")
    return branch


def command_exists(command: str) -> bool:
    """Return whether the executable at the start of a configured command exists.

    Agent/reviewer settings are command lines, not bare executable names.  Keep
    validation shell-free, but resolve absolute paths (including npm symlinks)
    explicitly and fall back to PATH lookup for portable commands.
    """
    try:
        executable = shlex.split(command)[0]
    except (ValueError, IndexError):
        return False
    candidate = Path(executable).expanduser()
    if candidate.is_absolute():
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return True
        except OSError:
            pass
        # ponytail: absolute path is a stale npm/nvm symlink target; fall back
        # to PATH by basename so a reinstalled/relinked CLI still resolves.
        return shutil.which(candidate.name) is not None
    return shutil.which(executable) is not None


def escalate_model_command(command: str) -> tuple[str, str | None, str | None]:
    """Promote a configured Codex/Claude command by one model tier."""
    codex = re.search(r"(-c\s+['\"]model=[\"'])([^\"']+)([\"']['\"])", command)
    if codex:
        current = codex.group(2)
        # "gpt-5.6-sol" is one Codex model spanning two escalation tiers (low
        # and high reasoning effort). The model= flag alone can't tell them
        # apart, so fold the reasoning effort into the lookup key.
        if current.lower() == "gpt-5.6-sol":
            effort = re.search(r"-c\s+['\"]model_reasoning_effort=[\"']([^\"']+)[\"']['\"]", command)
            if effort and effort.group(1).lower() == "high":
                current = "gpt-5.6-sol-high"
        upgraded = MODEL_ESCALATION.get(current.lower())
        if upgraded:
            return command[:codex.start(2)] + upgraded + command[codex.end(2):], current, upgraded
        return command, current, None

    claude = re.search(r"--model\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))", command)
    if claude:
        group = next(index for index in (1, 2, 3) if claude.group(index) is not None)
        current = claude.group(group)
        upgraded = MODEL_ESCALATION.get(current.lower())
        if upgraded:
            return command[:claude.start(group)] + upgraded + command[claude.end(group):], current, upgraded
        return command, current, None
    return command, None, None


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    stdin_text: str | None = None,
    timeout: int | None = 3600,
    log: Callable[[str], None] | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
    display_args: list[str] | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> CommandResult:
    # Some providers accept the full prompt as a positional argument. Keep that
    # private context out of logs and exception strings while executing real args.
    printable = " ".join(shlex.quote(part) for part in (display_args or args))
    if log:
        log(f"$ {printable}")
    try:
        process = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        raise WorkflowError(f"Could not run command: {printable}: {exc}") from exc

    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    if process.stdin is not None:
        try:
            process.stdin.write(stdin_text or "")
            process.stdin.close()
        except BrokenPipeError:
            pass

    output_parts: list[str] = []
    deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
    stream_closed = False
    while not stream_closed:
        if should_abort is not None and should_abort():
            process.kill()
            process.wait()
            raise WorkflowError("Job stopped by user.")
        if deadline is None:
            remaining = 0.25
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise WorkflowError(f"Command timed out after {timeout} seconds: {printable}")
        try:
            line = lines.get(timeout=min(0.25, remaining))
        except queue.Empty:
            continue
        if line is None:
            stream_closed = True
            continue
        output_parts.append(line)
        if log:
            log(line.rstrip("\r\n"))

    returncode = process.wait()
    output = "".join(output_parts)
    result = CommandResult(args=args, returncode=returncode, stdout=output)
    if check and returncode != 0:
        tail = output.strip()[-1200:]
        detail = f"\nProvider output:\n{tail}" if tail else ""
        raise WorkflowError(f"Command failed with exit code {returncode}: {printable}{detail}")
    return result


def _augment_claude_command(args: list[str]) -> list[str]:
    # `claude -p` with the default text format prints nothing until it exits, so the
    # live log looks frozen. Force stream-json so each turn is emitted as it happens.
    if (
        args
        and os.path.basename(args[0]) == "claude"
        and "-p" in args
        and "--output-format" not in args
    ):
        return [*args, "--output-format", "stream-json", "--verbose"]
    return args


def _summarize_tool_use(block: dict) -> str:
    name = block.get("name", "tool")
    inp = block.get("input", {}) or {}
    detail = (
        inp.get("command")
        or inp.get("file_path")
        or inp.get("path")
        or inp.get("pattern")
        or inp.get("description")
        or inp.get("prompt")
        or inp.get("url")
        or ""
    )
    detail = " ".join(str(detail).split())  # collapse newlines/whitespace
    if len(detail) > 160:
        detail = detail[:157] + "..."
    return f"⚙ {name}: {detail}" if detail else f"⚙ {name}"


def _telemetry_line(kind: str, **payload: object) -> str:
    """Keep multiline agent output as one structured event for the web decoder."""
    return json.dumps(
        {"mergequest_telemetry": 1, "kind": kind, **payload},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _bounded_telemetry_text(value: object, limit: int) -> str:
    """Bound verbose agent telemetry while retaining its beginning and outcome."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    marker = f"\n...[{len(text) - limit} chars omitted]...\n"
    available = max(0, limit - len(marker))
    head = (available * 2) // 3
    return text[:head].rstrip() + marker + text[-(available - head):].lstrip()


def _format_stream_json_line(line: str) -> str:
    # Turn one claude stream-json event into a readable log line ("" to drop it).
    stripped = line.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return line.rstrip("\r\n")
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return line.rstrip("\r\n")
    if not isinstance(event, dict):
        return ""
    etype = event.get("type")
    if etype == "system":
        model = event.get("model", "")
        return _telemetry_line("session_started", model=model) if model else ""
    if etype == "assistant":
        parts: list[str] = []
        message = event.get("message") or {}
        if not isinstance(message, dict):
            return ""
        for block in message.get("content", []) or []:
            # Claude has emitted plain string content in some stream-json
            # versions. Do not let the friendly logger hide the real result.
            if isinstance(block, str):
                if block.strip():
                    parts.append(_telemetry_line(
                        "agent_update", message=_bounded_telemetry_text(block, 1200),
                    ))
                continue
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(_telemetry_line(
                    "agent_update", message=_bounded_telemetry_text(block["text"], 1200),
                ))
            elif block.get("type") == "tool_use":
                inp = block.get("input") or {}
                command = inp.get("command") or _summarize_tool_use(block)
                parts.append(_telemetry_line(
                    "command_started", command=_bounded_telemetry_text(command, 320),
                ))
        return "\n".join(parts)
    if etype == "result":
        return _telemetry_line(
            "turn_completed",
            turns=event.get("num_turns"),
            failed=bool(event.get("is_error")),
            message=_bounded_telemetry_text(event.get("result"), 1200) if event.get("is_error") else "",
        )
    return ""  # tool_result / user echoes — skip the noise


def _format_codex_json_line(line: str) -> str:
    """Turn a Codex ``--json`` event into readable live-feed text."""
    stripped = line.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return line.rstrip("\r\n")
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return line.rstrip("\r\n")
    if not isinstance(event, dict):
        return ""
    etype = event.get("type")
    if etype == "thread.started":
        thread_id = str(event.get("thread_id", ""))
        return _telemetry_line("session_started", session=thread_id[:18])
    if etype == "turn.started":
        return _telemetry_line("turn_started")
    if etype == "turn.completed":
        usage = event.get("usage") or {}
        return _telemetry_line("turn_completed", output_tokens=usage.get("output_tokens"))
    item = event.get("item") or {}
    if not isinstance(item, dict):
        return ""
    item_type = item.get("type")
    if item_type == "agent_message" and item.get("text"):
        return _telemetry_line(
            "agent_update", message=_bounded_telemetry_text(item["text"], 1200),
        )
    if item_type == "reasoning" and item.get("text"):
        return _telemetry_line(
            "agent_reasoning", message=_bounded_telemetry_text(item["text"], 700),
        )
    if item_type == "command_execution":
        command = " ".join(str(item.get("command") or "").split())
        if len(command) > 260:
            command = command[:257] + "..."
        if etype == "item.started":
            return _telemetry_line("command_started", command=command) if command else ""
        output = _bounded_telemetry_text(item.get("aggregated_output"), 2400)
        exit_code = item.get("exit_code")
        return _telemetry_line(
            "command_completed",
            command=command,
            output=output,
            exit_code=exit_code,
            status=item.get("status", "done"),
        )
    if item_type == "file_change" and etype == "item.completed":
        changes = item.get("changes") or []
        paths = [str(change.get("path")) for change in changes if isinstance(change, dict) and change.get("path")]
        return _telemetry_line("files_changed", paths=paths)
    return ""


def run_configured_command(
    command: str,
    *,
    cwd: Path,
    prompt: str,
    timeout: int,
    log: Callable[[str], None],
    should_abort: Callable[[], bool] | None = None,
) -> CommandResult:
    prompt = compress_agent_prompt(prompt)
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise WorkflowError(f"Invalid command configuration: {exc}") from exc
    if not args:
        raise WorkflowError("Agent command is empty.")
    executable = Path(args[0]).expanduser()
    if executable.is_absolute() and not (executable.is_file() and os.access(executable, os.X_OK)):
        # ponytail: stale npm/nvm symlink; resolve by basename via PATH instead
        # of failing outright, matching command_exists()'s fallback.
        resolved = shutil.which(executable.name)
        if resolved:
            args[0] = resolved
    args = _augment_claude_command(args)
    safe_env = os.environ.copy()
    for sensitive_name in ("GH_TOKEN", "GITHUB_TOKEN", "SECRET_KEY"):
        safe_env.pop(sensitive_name, None)
    safe_env["TICKET_AGENT_AUTOMATION"] = "1"
    effective_log = log
    if "stream-json" in args:
        def effective_log(line: str, _log: Callable[[str], None] = log) -> None:
            text = _format_stream_json_line(line)
            if text:
                _log(text)
    elif "--json" in args and os.path.basename(args[0]) == "codex":
        def effective_log(line: str, _log: Callable[[str], None] = log) -> None:
            text = _format_codex_json_line(line)
            if text:
                _log(text)
    return run_command(
        args, cwd=cwd, stdin_text=prompt,
        timeout=timeout, log=effective_log, env=safe_env,
        should_abort=should_abort,
    )


def compress_agent_prompt(prompt: str, max_chars: int | None = None) -> str:
    """Bound prompt size while retaining instructions and the latest evidence.

    Agent prompts often accumulate logs, diffs, and repeated review output. Keeping
    both ends preserves the task contract and the most recent failure/result without
    spending the model's context window on duplicated middle history.
    """
    if not prompt:
        return prompt
    # The generated prompts already carry bounded task context. Do not infer
    # complexity from boilerplate words such as "schema" or "migration": doing
    # so expanded every ordinary implementation prompt. Only a real, non-empty
    # coordinated sibling diff receives a small allowance so review evidence is
    # not silently discarded.
    try:
        configured = max_chars if max_chars is not None else int(os.getenv("PROMPT_MAX_CHARS", "4000"))
    except ValueError:
        configured = 4000
    has_sibling_diff = bool(re.search(
        r'"coordinated_repository_changes":\{.+?"diff":"[^"}]',
        prompt,
        re.DOTALL,
    ))
    limit = max(configured, 6000) if has_sibling_diff else configured
    limit = max(2000, min(limit, 12000))
    if len(prompt) <= limit:
        return prompt
    marker = "\n\n[MergeQuest prompt compression: context omitted.]\n\n"
    available = limit - len(marker)
    head = int(available * 0.62)
    tail = available - head
    return (
        prompt[:head]
        + marker
        + prompt[-tail:]
    )


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkflowError(f"Required agent output was not created: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Agent output is not valid JSON: {path}: {exc}") from exc


def dump_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def generate_test_plan(
    agent_command: str, cwd: Path, issue: dict, result_path: Path, timeout: int,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Ask the coding agent to derive repro/pass-verification steps from a ticket's
    text alone (read-only, no repository access) and return {repro_steps, pass_steps}."""
    from prompts import test_plan_prompt  # local import: prompts.py does not depend on core.py

    result_path.unlink(missing_ok=True)
    run_configured_command(
        agent_command, cwd=cwd, prompt=test_plan_prompt(issue, str(result_path)), timeout=timeout,
        log=log or (lambda _line: None),
    )
    plan = load_json(result_path)
    ensure_keys(plan, ["repro_steps", "pass_steps"], "Test plan")
    return {
        "repro_steps": [str(item) for item in plan["repro_steps"]],
        "pass_steps": [str(item) for item in plan["pass_steps"]],
    }


def parse_validation_commands(raw: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            commands.append(shlex.split(line))
        except ValueError as exc:
            raise ValueError(f"Invalid validation command {line!r}: {exc}") from exc
    return commands


def markdown_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def format_review_markdown(review: dict) -> str:
    verdict = str(review.get("verdict", "COMMENT")).upper()
    summary = str(review.get("summary", "Automated review completed."))
    findings = review.get("findings") or []
    lines = ["## Automated code review", "", f"**Verdict:** `{verdict}`", "", summary]
    if findings:
        lines.extend(["", "| Severity | Location | Finding |", "|---|---|---|"])
        for item in findings:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or "General"
            line = item.get("line")
            location = f"`{path}:{line}`" if line else f"`{path}`"
            title = item.get("title") or item.get("body") or "Review finding"
            lines.append(
                f"| {markdown_escape(str(item.get('severity', 'INFO')).upper())} "
                f"| {markdown_escape(location)} | {markdown_escape(title)} |"
            )
    else:
        lines.extend(["", "No blocking findings were reported by the automated reviewer."])
    lines.extend(["", "> This is an automated review. A human reviewer should still approve the PR before merge."])
    return "\n".join(lines)


def blocking_findings(review: dict) -> list[dict]:
    findings = [
        item
        for item in (review.get("findings") or [])
        if isinstance(item, dict)
        if str(item.get("severity", "")).upper() in {"CRITICAL", "HIGH", "BLOCKER"}
    ]
    if str(review.get("verdict", "")).upper() == "BLOCK" and not findings:
        findings.append(
            {
                "severity": "HIGH",
                "title": "Reviewer blocked the change",
                "body": review.get("summary", "The automated reviewer returned BLOCK."),
                "path": None,
                "line": None,
            }
        )
    return findings


def ensure_keys(data: dict, keys: Iterable[str], label: str) -> None:
    missing = [key for key in keys if key not in data]
    if missing:
        raise WorkflowError(f"{label} is missing required fields: {', '.join(missing)}")


def working_tree_fingerprint(repo_dir: Path) -> str:
    """Hash tracked diffs and untracked file contents, excluding agent metadata."""
    digest = hashlib.sha256()
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=repo_dir,
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if diff.returncode != 0:
        raise WorkflowError("Could not fingerprint the working tree.")
    digest.update(diff.stdout)
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=repo_dir,
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if untracked.returncode != 0:
        raise WorkflowError("Could not enumerate untracked files for review isolation.")
    for raw_path in sorted(path for path in untracked.stdout.split(b"\0") if path):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        if relative == ".ticket-agent" or relative.startswith(".ticket-agent/"):
            continue
        digest.update(raw_path)
        file_path = repo_dir / relative
        if file_path.is_file():
            digest.update(file_path.read_bytes())
    return digest.hexdigest()
