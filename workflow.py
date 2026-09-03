from __future__ import annotations

import os
import json
import re
import shutil
import shlex
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

from config import Settings
from core import (
    WorkflowError,
    blocking_findings,
    command_exists,
    escalate_model_command,
    ensure_keys,
    format_review_markdown,
    load_json,
    make_branch_name,
    parse_issue_url,
    parse_validation_commands,
    run_command,
    run_configured_command,
    validate_ref_name,
    working_tree_fingerprint,
)
from github_ops import GitHubOps, is_schema_file
from prompts import automated_qa_prompt, confidence_gate_prompt, investigation_prompt, repair_prompt, review_prompt
from store import JobStore


class WorkflowRunner:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store
        # Keep expensive agent/repository work bounded while allowing the UI to
        # launch a small batch of tickets at once.
        self._run_slots = threading.BoundedSemaphore(3)
        # LOCAL_REPO_PATH jobs share branches and working-tree state. They must
        # never overlap, even though independently cloned jobs may run in parallel.
        self._local_workspace_lock = threading.Lock()

    def start(self, job_id: str) -> None:
        thread = threading.Thread(target=self._run_with_slot, args=(job_id,), daemon=True)
        thread.start()

    def start_testing(self, job_id: str) -> None:
        thread = threading.Thread(target=self._run_testing_with_slot, args=(job_id,), daemon=True)
        thread.start()

    def _run_testing_with_slot(self, job_id: str) -> None:
        with self._run_slots:
            if self.settings.local_repo_path:
                with self._local_workspace_lock:
                    self.run_testing(job_id)
            else:
                self.run_testing(job_id)

    def run_testing(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        params = job["parameters"]
        log = lambda message: self.log(job_id, message)
        self.store.update(job_id, status="running", stage="Reading ticket")
        result_path: Path | None = None
        original_refs: dict[Path, str] = {}
        prepared_prs: dict[str, dict] = {}
        github: GitHubOps | None = None
        try:
            if not self.settings.local_repo_path:
                raise WorkflowError("Testing Lab requires LOCAL_REPO_PATH to point to CRM_APP_PVF.")
            workspace_dir = self.settings.local_repo_path
            if not workspace_dir.exists():
                raise WorkflowError(f"CRM_APP_PVF workspace was not found at {workspace_dir}.")
            issue_ref = parse_issue_url(params["issue_url"])
            # Evidence Review escalates through Haiku, Claude, and GPT tiers on failure.
            # Kilo/Hy3 are skipped. Not user-selectable.
            command = self.settings.claude_command
            if not command_exists(command.split()[0]):
                raise WorkflowError(f"Agent executable is not installed or on PATH: {command}")

            github = GitHubOps(self.settings.command_timeout_seconds, log)
            github.check_auth()
            # The Integrity Scanner is also used to spot-check a fix that has
            # already shipped (ticket closed, PR merged), not only active work.
            issue = github.get_issue(issue_ref, require_open=False)
            if issue.get("state") != "open":
                log(f"Ticket is {issue.get('state', 'closed')}; running as a post-merge spot check.")
            repos = [path for path in (workspace_dir / "crm-staff-desktop", workspace_dir / "crm-api") if (path / ".git").exists()]
            if not repos and (workspace_dir / ".git").exists():
                repos = [workspace_dir]
            if not repos:
                raise WorkflowError("No CRM git repositories were found in the configured workspace.")
            repository_names = list(dict.fromkeys([
                issue_ref.repo,
                *(path.name for path in repos if path.name in {"crm-staff-desktop", "crm-api"}),
            ]))
            self.stage(job_id, "Preparing linked PR branches")
            pr_targets: list[tuple[Path, object, dict, str]] = []
            dirty_pr_repositories: list[str] = []
            for repo_dir in repos:
                repo_ref = type(issue_ref)(
                    owner=issue_ref.owner, repo=repo_dir.name, number=issue_ref.number,
                )
                pr = github.linked_open_pr(repo_ref, issue_ref, required=False)
                if not pr:
                    log(f"No linked open PR in {repo_dir.name}; leaving its current branch unchanged.")
                    continue
                if github.has_changes(repo_dir):
                    dirty_pr_repositories.append(repo_dir.name)
                    continue
                current_ref = run_command(
                    ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                    cwd=repo_dir, timeout=self.settings.command_timeout_seconds,
                    log=log, check=False,
                ).stdout.strip()
                if not current_ref:
                    current_ref = run_command(
                        ["git", "rev-parse", "HEAD"], cwd=repo_dir,
                        timeout=self.settings.command_timeout_seconds, log=log,
                    ).stdout.strip()
                pr_targets.append((repo_dir, repo_ref, pr, current_ref))

            if dirty_pr_repositories:
                raise WorkflowError(
                    "Cannot prepare linked PR branches because these repositories already have "
                    "uncommitted changes: " + ", ".join(dirty_pr_repositories)
                    + ". Commit, stash, or discard those changes before running the Integrity Scanner."
                )

            # Resolve and preflight every linked repository before switching any
            # branch, so a dirty companion repo cannot leave a half-prepared pair.
            for repo_dir, repo_ref, pr, current_ref in pr_targets:
                github.checkout_pr_branch(repo_dir, repo_ref, pr)
                original_refs[repo_dir] = current_ref
                prepared_prs[repo_dir.name] = pr
                log(
                    f"Integrity target: {repo_dir.name} PR #{pr['number']} "
                    f"on `{pr['headRefName']}` ({pr['url']})."
                )
            if prepared_prs:
                log("Testing linked PR branch" + ("es" if len(prepared_prs) != 1 else "") + ": " + ", ".join(
                    f"{name}=`{pr['headRefName']}`" for name, pr in prepared_prs.items()
                ))
            else:
                log("No linked open PR branches were found; treating the ticket as merged or branch-independent.")
            before = {path: working_tree_fingerprint(path) for path in repos}

            self._open_editor(workspace_dir, log)
            result_path = workspace_dir / ".ticket-agent" / f"qa-{job_id}.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            prompt = automated_qa_prompt(issue, str(result_path))
            max_testing_attempts = getattr(self.settings, "testing_max_attempts", 3)
            attempt = 0
            while True:
                attempt += 1
                provider_label = self._command_label(command)
                self.stage(job_id, f"Running autonomous QA with {provider_label}")
                log(
                    f"QA attempt {attempt}/{max_testing_attempts} started with "
                    f"{provider_label}; watchdog "
                    f"{getattr(self.settings, 'testing_pass_timeout_seconds', 600)}s."
                )
                result_path.unlink(missing_ok=True)
                try:
                    run_configured_command(
                        command, cwd=workspace_dir,
                        prompt=prompt,
                        timeout=getattr(
                            self.settings,
                            "testing_pass_timeout_seconds",
                            self.settings.command_timeout_seconds,
                        ),
                        log=log,
                        should_abort=lambda: self._should_abort(job_id),
                    )
                    break
                except WorkflowError as exc:
                    if self._testing_result_file_is_ready(result_path):
                        log(
                            "QA provider stopped after writing a valid report; "
                            "using the completed report instead of escalating."
                        )
                        break
                    if self._should_abort(job_id) or attempt >= max_testing_attempts:
                        raise
                    promoted_command, previous_model, promoted_model = self._escalate_command(command)
                    if not promoted_model:
                        raise
                    handoff = self._testing_escalation_handoff(job_id, result_path, exc)
                    command = promoted_command
                    log(f"Evidence Review escalation: {previous_model} -> {promoted_model} after: {exc}")
                    prompt = f"{prompt}\n\n{handoff}"
            provider = self._command_label(command)
            result = self._load_testing_result(job_id, result_path, log)
            ensure_keys(result, ["summary", "overall", "tests_run"], "QA result")
            reported_tests = self._dict_items(result.get("tests_run"))
            forbidden_frontend_checks = [
                item for item in reported_tests
                if self._is_autonomous_frontend_test(item)
            ]
            if forbidden_frontend_checks:
                commands = ", ".join(
                    str(item.get("command") or "frontend automation")
                    for item in forbidden_frontend_checks[:3]
                )
                raise WorkflowError(
                    "Integrity Scanner does not permit autonomous frontend testing: " + commands
                )
            tests = [item for item in reported_tests if not self._is_manual_ui_skip(item)]
            excluded_ui_checks = len(reported_tests) - len(tests)
            if excluded_ui_checks:
                log(f"Excluded {excluded_ui_checks} manual UI verification item(s) from automated QA evidence.")
            if not tests:
                raise WorkflowError("The QA agent reported no relevant non-UI automated test evidence.")
            statuses = [str(item.get("result", "")).lower() for item in tests]
            result["overall"] = self._qa_outcome(statuses)
            qa_passed = result["overall"] == "passed"
            # A stop is authoritative even when the CLI child reaches its natural
            # end after the operator pressed Stop. Never post or mark that run
            # completed after cancellation.
            if (self.store.get(job_id) or {}).get("status") == "stopped":
                log("QA child exited after stop; discarding its report.")
                return
            changed = [str(path) for path in repos if working_tree_fingerprint(path) != before[path]]
            if changed:
                raise WorkflowError("Testing-only run changed the workspace: " + ", ".join(changed))

            self.stage(job_id, "Posting QA evidence to GitHub")
            artifact_dir = self.settings.workspace_root / job_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            rows = "\n".join(
                f"- [{'x' if str(item.get('result')).lower() == 'passed' else ' '}] "
                f"**{str(item.get('result', 'skipped')).upper()}** — `{item.get('command', 'check')}`"
                + (f" — {item.get('notes')}" if item.get('notes') else "") for item in tests
            )
            report = (
                f"## Autonomous QA report — {str(result['overall']).upper()}\n\n"
                f"{result['summary']}\n\n### Machine-verified evidence\n{rows}\n\n"
                f"> Executed independently by MergeQuest Testing Lab using {provider}; no production files were changed."
            )
            github.comment_on_issue(issue_ref, report, artifact_dir)
            project_status: dict = {
                "updated": False, "count": 0, "test_state_count": 0,
                "status": None, "test_state": None,
            }
            if qa_passed:
                try:
                    project_status = github.sync_successful_qa_project_fields(
                        issue_ref, repository_names,
                    )
                    if not project_status["updated"]:
                        if project_status["has_open_pr"]:
                            project_status["warning"] = (
                                "The project is missing a PR Ready Status or Test State field."
                            )
                        else:
                            project_status["warning"] = (
                                "The project is missing a Done Status or Pass Test State option."
                            )
                    test_state_label = (
                        "null" if project_status["test_state"] is None
                        else project_status["test_state"]
                    )
                    log(
                        f"GitHub project sync: Status {project_status['status']}="
                        f"{project_status['count']}, Test State {test_state_label}="
                        f"{project_status['test_state_count']}."
                    )
                except Exception as exc:
                    project_status["warning"] = str(exc)
                    log(f"QA passed, but GitHub project fields could not update: {exc}")
            final = {
                **result,
                "tests_run": tests,
                "provider": provider,
                "ticket_url": params["issue_url"],
                "repositories": repository_names,
                "tested_prs": prepared_prs,
                "override_allowed": True,
                "automated_overall": result.get("overall"),
                "project_status": project_status,
            }
            self.store.update(job_id, status="completed", stage="QA report posted", result_json=final)
            log("Completed: autonomous QA evidence posted to the ticket.")
            notification_result = "PASS" if qa_passed else "FAIL"
            self._send_windows_notification(
                f"MergeQuest: Testing {notification_result} #{issue_ref.number}",
                result.get("summary", ""),
                log,
                launch_url=params["issue_url"],
            )
        except Exception as exc:
            if (self.store.get(job_id) or {}).get("status") != "stopped":
                self.store.update(job_id, status="failed", stage="QA scan failed", error=str(exc))
                self._send_windows_notification(
                    f"MergeQuest: Testing FAIL #{issue_ref.number}", str(exc), log,
                    launch_url=params.get("issue_url"),
                )
            log(f"QA scan failed: {exc}")
        finally:
            if result_path:
                result_path.unlink(missing_ok=True)
            if github:
                for repo_dir, original_ref in reversed(list(original_refs.items())):
                    try:
                        if github.has_changes(repo_dir):
                            log(
                                f"Could not restore {repo_dir.name} to `{original_ref}` because "
                                "the testing process left uncommitted changes."
                            )
                            continue
                        run_command(
                            ["git", "checkout", original_ref], cwd=repo_dir,
                            timeout=self.settings.command_timeout_seconds, log=log,
                        )
                        log(f"Restored {repo_dir.name} to `{original_ref}` after QA.")
                    except Exception as restore_exc:
                        log(f"Could not restore {repo_dir.name} after QA: {restore_exc}")

    def _run_with_slot(self, job_id: str) -> None:
        with self._run_slots:
            if self.settings.local_repo_path:
                with self._local_workspace_lock:
                    self.run(job_id)
            else:
                self.run(job_id)

    def log(self, job_id: str, message: str) -> None:
        self.store.append_log(job_id, message)

    def _should_abort(self, job_id: str) -> bool:
        """Poll the store so a user-requested stop kills the live subprocess
        immediately instead of waiting out its full command timeout, which
        would otherwise hold a run slot hostage and block the queue."""
        job = self.store.get(job_id)
        return bool(job and job["status"] == "stopped")

    def _command_label(self, command: str) -> str:
        """Best-effort human label for a configured command, for logs, the
        posted QA report, and the pipeline progress indicator. Includes the
        reasoning effort for "gpt-5.6-sol" since low/high are distinct
        pipeline tiers that share one underlying model name."""
        _, previous_model, _ = escalate_model_command(command)
        if previous_model == "gpt-5.6-sol":
            effort = re.search(r"-c\s+['\"]model_reasoning_effort=[\"']([^\"']+)[\"']['\"]", command)
            if effort and effort.group(1).lower() == "high":
                return "gpt-5.6-sol-high"
        if previous_model:
            return previous_model
        try:
            return Path(shlex.split(command)[0]).name
        except (ValueError, IndexError):
            return "agent"

    def _provider_command(self, provider: str, custom: str | None, role: str) -> str:
        if provider == "claude":
            return self.settings.claude_command
        if provider == "codex":
            return self.settings.agent_command if role == "agent" else self.settings.review_command
        if provider == "opencode":
            # The model picker submits the selected command as a custom value;
            # use it instead of replacing it with the default big-pickle command.
            return (custom or self.settings.opencode_command).strip()
        return (custom or "").strip()

    def _load_testing_result(self, job_id: str, result_path: Path, log) -> dict:
        try:
            return load_json(result_path)
        except WorkflowError as exc:
            if "Required agent output was not created" not in str(exc):
                raise
            recovered = self._recover_testing_result_from_logs(job_id)
            if recovered is None:
                raise
            log("Recovered QA verdict from provider telemetry after the JSON report file was not created.")
            return recovered

    @staticmethod
    def _testing_result_file_is_ready(result_path: Path) -> bool:
        try:
            result = load_json(result_path)
            ensure_keys(result, ["summary", "overall", "tests_run"], "QA result")
        except WorkflowError:
            return False
        return True

    def _recover_testing_result_from_logs(self, job_id: str) -> dict | None:
        job = (
            self.store.get_with_logs(job_id)
            if hasattr(self.store, "get_with_logs")
            else self.store.get(job_id)
        ) or {}
        logs = str(job.get("logs") or "")
        if not logs and hasattr(self.store, "logs"):
            logs = "\n".join(str(item) for item in getattr(self.store, "logs"))
        if not logs.strip():
            return None
        text = self._telemetry_text(logs)
        match = re.search(
            r"QA result:\s*(?:\*\*)?\s*(PASS|FAIL|FAILED|INCOMPLETE)\b",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        token = match.group(1).lower()
        overall = "passed" if token == "pass" else ("incomplete" if token == "incomplete" else "failed")
        evidence_match = re.search(
            r"Evidence:\s*(.*?)(?:\n\s*(?:No production files|Temporary proof|Agent turn|Windows notification|QA scan failed)|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        evidence = evidence_match.group(1).strip() if evidence_match else ""
        evidence_lines = [
            re.sub(r"^\s*[-*]\s*", "", line).strip()
            for line in evidence.splitlines()
            if re.sub(r"^\s*[-*]\s*", "", line).strip()
        ]
        notes = self._bounded_text(
            " ".join(evidence_lines)
            or "Provider reported a QA verdict in telemetry but did not create the JSON report file.",
            900,
        )
        return {
            "summary": self._bounded_text(
                evidence_lines[0] if evidence_lines else f"Provider reported QA {overall}.",
                500,
            ),
            "overall": overall,
            "tests_run": [{
                "command": "Recovered provider QA telemetry verdict",
                "result": overall if overall in {"passed", "failed"} else "skipped",
                "notes": notes,
            }],
        }

    @staticmethod
    def _telemetry_text(logs: str) -> str:
        messages: list[str] = []
        for line in str(logs or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    messages.append(line)
                    continue
                if isinstance(event, dict) and event.get("mergequest_telemetry"):
                    if event.get("message"):
                        messages.append(str(event["message"]))
                    elif event.get("command"):
                        messages.append(str(event["command"]))
                    continue
            messages.append(line)
        return "\n".join(messages)

    def _testing_escalation_handoff(
        self, job_id: str, result_path: Path, failure: Exception,
    ) -> str:
        """Carry prior QA investigation forward when a higher tier takes over."""
        job = (
            self.store.get_with_logs(job_id)
            if hasattr(self.store, "get_with_logs")
            else self.store.get(job_id)
        ) or {}
        logs = str(job.get("logs") or "")
        if not logs and hasattr(self.store, "logs"):
            logs = "\n".join(str(item) for item in getattr(self.store, "logs"))
        log_tail = self._bounded_text(logs, 12_000)
        partial_report = ""
        if result_path.exists():
            try:
                partial_report = self._bounded_text(result_path.read_text(encoding="utf-8"), 4_000)
            except OSError:
                partial_report = ""
        partial_section = (
            f"\n\nPARTIAL QA REPORT FOUND AT ESCALATION:\n{partial_report}"
            if partial_report.strip() else ""
        )
        return (
            "ESCALATION HANDOFF — CONTINUE, DO NOT RESTART\n"
            f"The prior QA model failed or timed out with: {self._bounded_text(str(failure), 1200)}\n"
            "Use the investigation trail below as completed work. Do not repeat broad discovery "
            "unless a specific fact is missing. Continue from the last useful file, command, or "
            "hypothesis, then write the required QA JSON report.\n\n"
            f"PRIOR QA TELEMETRY TAIL:\n{log_tail}"
            f"{partial_section}"
        )

    @staticmethod
    def _bounded_text(value: object, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        marker = f"\n...[{len(text) - limit} chars omitted]...\n"
        head = max(0, (limit - len(marker)) // 3)
        tail = max(0, limit - len(marker) - head)
        return text[:head].rstrip() + marker + text[-tail:].lstrip()

    def _escalate_command(self, command: str) -> tuple[str, str | None, str | None]:
        promoted_command, previous_model, promoted_model = escalate_model_command(command)
        if promoted_model and promoted_model.startswith("claude-"):
            return self._with_claude_model(self.settings.claude_command, promoted_model), previous_model, promoted_model
        if promoted_model and promoted_model.startswith("gpt-"):
            base = self.settings.agent_command
            # "gpt-5.6-sol-high" is the same model as "gpt-5.6-sol" at a higher
            # reasoning effort, not a distinct model name.
            model_name = "gpt-5.6-sol" if promoted_model == "gpt-5.6-sol-high" else promoted_model
            command = self._with_codex_model(base, model_name)
            if promoted_model == "gpt-5.6-sol":
                command = self._with_codex_reasoning_effort(command, "low")
            elif promoted_model == "gpt-5.6-sol-high":
                command = self._with_codex_reasoning_effort(command, "high")
            return command, previous_model, promoted_model
        return promoted_command, previous_model, promoted_model

    def _with_claude_model(self, command: str, model: str) -> str:
        model_flag = re.compile(r"--model\s+(?:\"[^\"]*\"|'[^']*'|\S+)")
        if model_flag.search(command):
            return model_flag.sub(f"--model {model}", command, count=1)
        return f"{command} --model {model}"

    def _with_codex_model(self, command: str, model: str) -> str:
        model_config = re.compile(r"-c\s+(['\"])model=[\"'][^\"']*[\"']\1")
        replacement = f"-c 'model=\"{model}\"'"
        if model_config.search(command):
            return model_config.sub(replacement, command, count=1)
        parts = shlex.split(command)
        if len(parts) >= 2 and parts[1] == "exec":
            return " ".join([shlex.quote(parts[0]), "exec", replacement, *[shlex.quote(part) for part in parts[2:]]])
        return f"{command} {replacement}"

    def _with_codex_reasoning_effort(self, command: str, effort: str) -> str:
        effort_config = re.compile(r"-c\s+(['\"])model_reasoning_effort=[\"'][^\"']*[\"']\1")
        replacement = f"-c 'model_reasoning_effort=\"{effort}\"'"
        if effort_config.search(command):
            return effort_config.sub(replacement, command, count=1)
        parts = shlex.split(command)
        if len(parts) >= 2 and parts[1] == "exec":
            return " ".join([shlex.quote(parts[0]), "exec", replacement, *[shlex.quote(part) for part in parts[2:]]])
        return f"{command} {replacement}"

    def stage(self, job_id: str, name: str) -> None:
        self.store.update(job_id, stage=name)
        confidence = self._stage_confidence(name)
        if confidence is not None:
            self._publish_live_confidence(job_id, confidence, name)
        self.log(job_id, f"\n== {name} ==")

    @staticmethod
    def _stage_confidence(name: str) -> float | None:
        """Provide a directional confidence signal while the final report is pending."""
        normalized = name.lower()
        milestones = (
            ("starting", 0.20), ("checking github", 0.28), ("reading ticket", 0.34),
            ("cloning repository", 0.32), ("preparing", 0.38),
            ("investigating", 0.52), ("repairing", 0.48),
            ("running validation", 0.68), ("re-running validation", 0.62),
            ("reviewing", 0.76), ("committing", 0.88), ("pushing", 0.90),
            ("creating pull request", 0.93), ("posting code review", 0.95),
            ("linking original ticket", 0.97), ("completed", 1.0),
        )
        for marker, score in milestones:
            if marker in normalized:
                return score
        return None

    def _publish_live_confidence(self, job_id: str, score: float, label: str) -> None:
        """Persist provisional confidence so the job page can update during execution."""
        job = self.store.get(job_id)
        if not job:
            return
        result = dict(job.get("result") or {})
        previous = result.get("confidence")
        result["confidence"] = max(0.0, min(1.0, round(score, 2)))
        if isinstance(previous, (int, float)):
            result["confidence_delta"] = round(result["confidence"] - float(previous), 2)
        result["confidence_live"] = True
        result["confidence_label"] = label
        self.store.update(job_id, result_json=result)

    def publish_agent_confidence(self, job_id: str, score: object, label: str = "Agent assessment") -> None:
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            return
        self._publish_live_confidence(job_id, confidence, label)

    def _publish_prompt_tokens(self, job_id: str, prompt: str) -> None:
        """Expose the compressed input budget while a model pass is running."""
        job = self.store.get(job_id)
        if not job:
            return
        result = dict(job.get("result") or {})
        prompt_tokens = max(1, round(len(prompt) / 4))
        result["prompt_tokens"] = prompt_tokens
        result["session_prompt_tokens"] = int(result.get("session_prompt_tokens") or 0) + prompt_tokens
        result["session_tokens"] = int(result.get("session_prompt_tokens") or 0) + int(result.get("session_output_tokens") or 0)
        self.store.update(job_id, result_json=result)

    @contextmanager
    def _timed_stage(self, job_id: str, stage_name: str):
        """Record wall-clock duration for a stage and flag it if it ran long."""
        started = time.time()
        try:
            yield
        finally:
            duration_ms = int((time.time() - started) * 1000)
            self.store.record_stage_timing(job_id, stage_name, duration_ms)
            threshold = self.settings.stage_stall_threshold_ms
            if duration_ms > threshold:
                self.log(
                    job_id,
                    f"⚠️ stage '{stage_name}' ran long ({duration_ms}ms, "
                    f"threshold {threshold}ms) — possible stall",
                )

    @staticmethod
    def _estimate_ticket_risk(issue: dict) -> str:
        """Cheap pre-implementation heuristic: no extra GitHub calls, just what's
        already on the issue dict. Used to pick a starting model tier instead of
        always burning the bottom rung of the escalation ladder."""
        low_risk_labels = {
            "size:small", "size:xs", "good first issue", "typo", "chore", "documentation",
        }
        labels = {
            str(label.get("name") or "").strip().lower()
            for label in (issue.get("labels") or [])
            if isinstance(label, dict)
        }
        if labels & low_risk_labels:
            return "low"
        body = str(issue.get("body") or "")
        title = str(issue.get("title") or "")
        bullet_count = len(re.findall(r"^\s*[-*]\s+", body, re.MULTILINE))
        if len(body) < 400 and len(title) < 80 and bullet_count <= 3:
            return "low"
        return "normal"

    def _run_validation_and_review(
        self, job_id: str, changed_repos: dict[str, Path],
        validation_commands: list[list[str]], github: GitHubOps, log,
        issue: dict, result: dict, base_branch: str, review_command: str,
        delivery_started_at: float | None,
    ) -> tuple[list[dict], dict, dict[str, dict]]:
        """Integrity checks and independent review both operate on the same
        post-implement repo state and don't depend on each other; run them
        concurrently instead of paying their cost serially."""
        with ThreadPoolExecutor(max_workers=2) as executor:
            integrity_future = executor.submit(
                self._run_integrity_checks, changed_repos, validation_commands, github, log,
            )
            review_future = executor.submit(
                self._review_changed_repositories,
                job_id, changed_repos, issue, result, base_branch, review_command, log,
                delivery_started_at=delivery_started_at,
            )
            integrity_checks = integrity_future.result()
            reviews = review_future.result()
        review: dict = {"verdict": "PASS", "summary": "All changed repositories reviewed.", "findings": []}
        for repo_name, repo_review in reviews.items():
            for finding in self._dict_items(repo_review.get("findings")):
                review["findings"].append({**finding, "repository": repo_name})
        if review["findings"]:
            review["verdict"] = "BLOCK" if blocking_findings(review) else "COMMENT"
        return integrity_checks, review, reviews

    @staticmethod
    def _review_fingerprint(review: dict) -> tuple:
        return tuple(sorted(
            str(item.get("summary") or item.get("title") or "")
            for item in blocking_findings(review)
        ))

    def approve_stage(self, job_id: str, name: str) -> None:
        job = self.store.get(job_id)
        if job and job["status"] == "stopped":
            raise WorkflowError("Job stopped by user.")
        if not job or job["parameters"].get("approval_mode") != "each_stage":
            return
        self.store.request_approval(job_id, f"Proceed with stage: {name}?")
        while True:
            current = self.store.get(job_id)
            if not current or current["status"] == "stopped":
                raise WorkflowError("Job stopped while waiting for approval.")
            if current.get("approval_state") == "approved":
                self.store.update(job_id, status="running", approval_state="auto", approval_message="")
                return
            if current.get("approval_state") == "rejected":
                raise WorkflowError(f"Stage rejected by user: {name}")
            time.sleep(1)

    def run(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        params = job["parameters"]
        def log(message: str) -> None:
            self.log(job_id, message)
        self.store.update(job_id, status="running", stage="Starting")
        result: dict = {}
        review: dict = {}
        started_at = time.time()

        try:
            is_qa_fix = params.get("workflow_profile") == "qa_fix"
            issue_ref = parse_issue_url(params["issue_url"])
            base_branch = validate_ref_name(params["base_branch"], "base branch")
            branch_prefix = params.get("branch_prefix", "feature")
            agent_provider = params.get("agent_provider") or ("custom" if params.get("agent_command") else "codex")
            review_provider = params.get("review_provider") or ("custom" if params.get("review_command") else "codex")
            agent_command = self._provider_command(agent_provider, params.get("agent_command"), "agent")
            review_command = self._provider_command(review_provider, params.get("review_command"), "review")
            validation_commands = parse_validation_commands(params.get("validation_commands", ""))
            delivery_started_at = started_at if params.get("workflow_profile", "full_pr") == "full_pr" else None

            for required in ("git", "gh"):
                if shutil.which(required) is None:
                    raise WorkflowError(f"Required executable is not installed or on PATH: {required}")
            if not command_exists(agent_command):
                raise WorkflowError(f"Agent executable is not installed or on PATH: {agent_command}")
            if not command_exists(review_command):
                raise WorkflowError(f"Review executable is not installed or on PATH: {review_command}")

            artifact_dir = self.settings.workspace_root / job_id
            workspace_dir: Path
            if is_qa_fix and not self.settings.local_repo_path:
                raise WorkflowError("Fix and retest requires LOCAL_REPO_PATH to point to CRM_APP_PVF.")
            if self.settings.local_repo_path:
                local_root = self.settings.local_repo_path
                # LOCAL_REPO_PATH may point at an application workspace containing
                # several repositories. Resolve the ticket's repository beneath it.
                child_repo = local_root / issue_ref.repo
                # Prefer a nested repository matching the GitHub ticket. The
                # application workspace may itself be a Git checkout with a
                # different remote, so checking only local_root/.git is unsafe.
                repo_dir = child_repo if (child_repo / ".git").exists() else local_root
                if not (repo_dir / ".git").exists():
                    raise WorkflowError(
                        f"Local repository was not found at {repo_dir}. "
                        "Set LOCAL_REPO_PATH to the application workspace or repository root."
                    )
                workspace_dir = local_root
            else:
                repo_dir = artifact_dir / "repo"
                workspace_dir = repo_dir
            artifact_dir.mkdir(parents=True, exist_ok=True)
            github = GitHubOps(self.settings.command_timeout_seconds, log)
            if delivery_started_at is not None:
                review_budget, publish_budget = self._full_delivery_reserves()
                log(
                    "Full Delivery target: "
                    f"{getattr(self.settings, 'full_delivery_target_seconds', 180)} seconds "
                    f"with {getattr(self.settings, 'agent_pass_timeout_seconds', self.settings.command_timeout_seconds)}s agent passes "
                    f"and effective reserves of {review_budget}s for review plus "
                    f"{publish_budget}s for publication."
                )

            self.approve_stage(job_id, "Checking GitHub access")
            with self._timed_stage(job_id, "Checking GitHub access"):
                self.stage(job_id, "Checking GitHub access")
                github.check_auth()

            self.approve_stage(job_id, "Reading ticket")
            with self._timed_stage(job_id, "Reading ticket"):
                self.stage(job_id, "Reading ticket")
                issue = github.get_issue_with_compact_context(issue_ref)
            existing_prs: dict[str, dict] = {}
            if is_qa_fix:
                source_pr = github.linked_open_pr(issue_ref)
                assert source_pr is not None
                existing_prs[issue_ref.repo] = source_pr
                base_branch = validate_ref_name(source_pr["baseRefName"], "PR base branch")
                branch_name = validate_ref_name(source_pr["headRefName"], "PR head branch")
                log(f"Fix/retest target: {source_pr['url']} ({branch_name} -> {base_branch})")
            else:
                branch_name = make_branch_name(branch_prefix, issue_ref.number, issue["title"])
            # Autonomous Daemon fixes the ticket immediately. Test plans are
            # generated only through the explicit Recon Protocol action.
            test_plan = self._get_cached_test_plan(issue_ref)

            repo_dirs: dict[str, Path] = {issue_ref.repo: repo_dir}
            if self.settings.local_repo_path and issue_ref.repo in {"crm-staff-desktop", "crm-api"}:
                for paired_name in ("crm-staff-desktop", "crm-api"):
                    paired_dir = workspace_dir / paired_name
                    if not (paired_dir / ".git").exists():
                        raise WorkflowError(f"Paired CRM repository was not found at {paired_dir}.")
                    repo_dirs[paired_name] = paired_dir

            with self._timed_stage(job_id, "Cloning and preparing repositories"):
                if not self.settings.local_repo_path:
                    self.approve_stage(job_id, "Cloning repository")
                    self.stage(job_id, "Cloning repository")
                    github.clone(issue_ref, repo_dir)
                resumable_repositories: set[str] = set()
                if not is_qa_fix:
                    resumable_repositories, conflicting_repositories = self._classify_dirty_repositories(
                        repo_dirs, branch_name, github, log,
                    )
                    for conflict_name in conflicting_repositories:
                        conflict_dir = repo_dirs[conflict_name]
                        previous_branch = github.current_branch(conflict_dir)
                        github.discard_changes(conflict_dir)
                        log(
                            f"Discarded leftover changes in {conflict_name} from `{previous_branch}` "
                            f"so `{branch_name}` starts on a clean workspace."
                        )
                if is_qa_fix:
                    for repo_name, current_repo_dir in repo_dirs.items():
                        log(f"Preparing repository: {repo_name}")
                        repo_ref = type(issue_ref)(owner=issue_ref.owner, repo=repo_name, number=issue_ref.number)
                        pr = existing_prs.get(repo_name)
                        if pr is None:
                            pr = github.linked_open_pr(repo_ref, issue_ref, required=False)
                            if pr:
                                existing_prs[repo_name] = pr
                        if pr:
                            if pr["baseRefName"] != base_branch:
                                raise WorkflowError(
                                    f"Linked PR {pr['url']} targets {pr['baseRefName']}, not {base_branch}."
                                )
                            github.checkout_pr_branch(current_repo_dir, repo_ref, pr)
                        else:
                            github.checkout_base_branch(current_repo_dir, base_branch)
                            log(f"No linked PR exists for {repo_name}; it is available for read-only investigation.")
                else:
                    prepare_targets = {
                        name: path for name, path in repo_dirs.items()
                        if name not in resumable_repositories
                    }
                    for repo_name in repo_dirs:
                        log(f"Preparing repository: {repo_name}")

                    def prepare_repository(current_repo_dir: Path) -> None:
                        # Dirty-state classification immediately above is the
                        # authoritative clean check; avoid another status process.
                        github.prepare_branch(
                            current_repo_dir, base_branch, branch_name, clean_checked=True,
                        )

                    if len(prepare_targets) == 1:
                        prepare_repository(next(iter(prepare_targets.values())))
                    elif prepare_targets:
                        with ThreadPoolExecutor(max_workers=min(3, len(prepare_targets))) as executor:
                            futures = [
                                executor.submit(prepare_repository, path)
                                for path in prepare_targets.values()
                            ]
                            for future in futures:
                                future.result()
            # When working from a local repository, open the enclosing application
            # workspace so both the desktop and API repositories are visible in VS Code.
            # Git operations and the coding agent remain scoped to repo_dir.
            editor_dir = (
                self.settings.local_repo_path
                if self.settings.local_repo_path
                else repo_dir
            )
            self._open_editor(editor_dir, log)

            if agent_provider == "codex" and not is_qa_fix and self._estimate_ticket_risk(issue) == "low":
                agent_command = self.settings.claude_command
                log(
                    "Fast path: ticket looks low-risk (short body/title, no checklist, "
                    "or a small-size label) — starting on the cheap model tier instead of "
                    "the default agent, escalating only if it fails."
                )

            self.approve_stage(job_id, "Investigating and implementing")
            with self._timed_stage(job_id, "Investigating and implementing"):
                self.stage(job_id, "Investigating and implementing")
                result_path = repo_dir / ".ticket-agent" / "result.json"
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.unlink(missing_ok=True)
                initial_prompt = investigation_prompt(
                    issue,
                    base_branch,
                    branch_name,
                    repositories=list(repo_dirs),
                    result_path=str(result_path),
                    run_validation=is_qa_fix,
                )
                (artifact_dir / "investigation-prompt.md").write_text(initial_prompt, encoding="utf-8")
                result = self._run_agent_gated(
                    job_id, agent_command, workspace_dir, result_path, issue, initial_prompt, log,
                    require_all_tests_passed=is_qa_fix,
                    auto_escalate_model=params.get("model_auto_escalate", True),
                    delivery_started_at=delivery_started_at,
                )
                changed_repos = {
                    name: path for name, path in repo_dirs.items() if github.has_changes(path)
                }
                if not changed_repos:
                    if result.get("safe_to_pr") is True and result.get("root_cause"):
                        # The agent investigated and concluded, with evidence, that this
                        # repository needs no code change (e.g. the real fix already landed
                        # elsewhere, or the bug lives entirely in another service). That is
                        # a legitimate outcome, not a failed run — report it and stop instead
                        # of raising a generic "no changes" error.
                        no_change_result = {
                            "ticket_url": issue["html_url"], "repository": issue_ref.full_name,
                            "issue_number": issue_ref.number, "base_branch": base_branch,
                            "branch_name": branch_name, "pr_url": None, "pr_urls": {},
                            "confidence": result["confidence"], "summary": result["summary"],
                            "root_cause": result["root_cause"], "code_written": False,
                            "no_change_needed": True,
                            "unresolved_risks": result.get("unresolved_risks") or [],
                            "completion_requirements": result.get("completion_requirements") or [],
                            "pr_notes": result.get("pr_notes") or "",
                        }
                        self.store.update(
                            job_id, status="completed", stage="No change needed",
                            result_json=no_change_result,
                        )
                        if params.get("comment_on_failure", self.settings.comment_on_failure):
                            try:
                                github.comment_on_issue(
                                    issue_ref,
                                    self._no_change_comment(result),
                                    artifact_dir,
                                )
                                log("Posted no-change-needed explanation on the original ticket.")
                            except Exception as comment_exc:  # noqa: BLE001 - best-effort notification
                                log(f"Could not post explanation on the ticket: {comment_exc}")
                        log("Completed: agent found no source change is needed in this repository.")
                        return
                    raise WorkflowError("The coding agent completed without producing any source changes.")
                log("Repositories changed: " + ", ".join(changed_repos))

            self.approve_stage(job_id, "Running validation")
            with self._timed_stage(job_id, "Running validation"):
                self.stage(job_id, "Running validation")
                agent_checks = self._dict_items(result.get("tests_run"))
                integrity_checks, review, reviews = self._run_validation_and_review(
                    job_id, changed_repos, validation_commands, github, log,
                    issue, result, base_branch, review_command, delivery_started_at,
                )
                result["tests_run"] = [*agent_checks, *integrity_checks]
                if not validation_commands:
                    log(
                        "No Integrity scripts were configured; completed diff integrity checks only. "
                        "Functional testing remains separate from the Autonomous Daemon fix stage."
                    )

            cycles = 0
            repair_command = agent_command
            repair_limit = (
                max(self.settings.max_repair_cycles, self.settings.max_gate_attempts - 1)
                if is_qa_fix else self.settings.max_repair_cycles
            )
            previous_fingerprint = self._review_fingerprint(review)
            while blocking_findings(review) and cycles < repair_limit:
                cycles += 1
                promoted_command, previous_model, promoted_model = self._escalate_command(repair_command)
                if promoted_model:
                    repair_command = promoted_command
                    log(
                        f"Model escalation: {previous_model} -> {promoted_model} "
                        "because independent review still has blocking findings."
                    )
                repair_stage = f"Repairing review findings ({cycles}/{repair_limit})"
                self.approve_stage(job_id, repair_stage)
                with self._timed_stage(job_id, "Repairing review findings"):
                    self.stage(job_id, repair_stage)
                    result = self._run_agent_gated(
                        job_id, repair_command, workspace_dir, result_path, issue,
                        repair_prompt(issue, review, str(result_path), run_validation=is_qa_fix), log,
                        require_all_tests_passed=is_qa_fix,
                        auto_escalate_model=params.get("model_auto_escalate", True),
                        delivery_started_at=delivery_started_at,
                    )
                    self.approve_stage(job_id, "Re-running validation")
                    self.stage(job_id, "Re-running validation")
                    changed_repos = {name: path for name, path in repo_dirs.items() if github.has_changes(path)}
                    agent_checks = self._dict_items(result.get("tests_run"))
                    integrity_checks, review, reviews = self._run_validation_and_review(
                        job_id, changed_repos, validation_commands, github, log,
                        issue, result, base_branch, review_command, delivery_started_at,
                    )
                    result["tests_run"] = [*agent_checks, *integrity_checks]
                fingerprint = self._review_fingerprint(review)
                if fingerprint and fingerprint == previous_fingerprint:
                    log(
                        "Repair did not change the blocking findings; stopping repair cycles "
                        "now instead of retrying the same failure again."
                    )
                    break
                previous_fingerprint = fingerprint

            blockers = blocking_findings(review)
            if blockers:
                titles = ", ".join(str(item.get("title", "blocking finding")) for item in blockers)
                raise WorkflowError(f"Automated review still has blocking findings: {titles}")

            if is_qa_fix:
                tests = self._dict_items(result.get("tests_run"))
                if not tests or any(str(item.get("result", "")).lower() != "passed" for item in tests):
                    raise WorkflowError("The 100% test gate failed; the open PR was not updated.")
                missing_prs = [name for name in changed_repos if name not in existing_prs]
                if missing_prs:
                    raise WorkflowError(
                        "The fix changed repositories without linked open PRs: " + ", ".join(missing_prs)
                        + ". No remote branches were updated."
                    )

            # Investigation mode deliberately leaves the implementation in the
            # workspace for local review. It performs the same safety gates as
            # Autopilot, but never commits, pushes, or opens a pull request.
            if params.get("workflow_profile") == "investigate_fix":
                blockers = blocking_findings(review)
                if blockers:
                    titles = ", ".join(str(item.get("title", "blocking finding")) for item in blockers)
                    raise WorkflowError(f"Automated review still has blocking findings: {titles}")
                local_result = {
                    "ticket_url": issue["html_url"], "repository": issue_ref.full_name,
                    "issue_number": issue_ref.number, "base_branch": base_branch,
                    "branch_name": branch_name, "pr_url": None, "pr_urls": {},
                    "confidence": result["confidence"], "summary": result["summary"],
                    "root_cause": result["root_cause"], "review": review,
                    "code_written": True, "pr_skipped": True,
                    "commit_message": result["commit_message"], "pr_title": result["pr_title"],
                    "reviews": reviews, "changed_repos": list(changed_repos),
                    "evidence": result.get("evidence") or [],
                    "files_changed": result.get("files_changed") or [],
                    "tests_run": result.get("tests_run") or [],
                    "unresolved_risks": result.get("unresolved_risks") or [],
                    "completion_requirements": result.get("completion_requirements") or [],
                    "pr_notes": result.get("pr_notes") or "",
                }
                self.store.update(job_id, status="completed", stage="Code written (PR skipped)", result_json=local_result)
                log("Completed: code written locally; PR creation skipped by strategy.")
                return

            with self._timed_stage(job_id, "Publishing pull request"):
                self._finish_pr(
                    job_id, params, log, issue_ref, issue, base_branch, branch_name,
                    changed_repos, result, review, reviews, test_plan, github, artifact_dir, started_at,
                    existing_prs=existing_prs if is_qa_fix else None,
                )
        except Exception as exc:  # noqa: BLE001 - workflow boundary must record every failure
            current = self.store.get(job_id)
            if not current or current.get("status") != "stopped":
                self.store.update(job_id, status="failed", stage="Failed", error=str(exc))
                if params.get("comment_on_failure", self.settings.comment_on_failure):
                    self._comment_on_failure(job_id, params, exc, current, result, review, log)
                issue_number = ""
                try:
                    issue_number = parse_issue_url(params["issue_url"]).number
                except Exception:  # noqa: BLE001 - notification is best-effort
                    pass
                self._send_windows_notification(
                    f"MergeQuest: PR failed #{issue_number}".strip(),
                    str(exc),
                    log,
                    launch_url=params.get("issue_url"),
                )
            log(f"ERROR: {exc}")

    def continue_to_pr(self, job_id: str) -> None:
        """Resume a job that stopped at 'Code written (PR skipped)' and run it
        through commit/push/PR/review/link using the code already on disk."""
        thread = threading.Thread(target=self._continue_to_pr_with_slot, args=(job_id,), daemon=True)
        thread.start()

    def _continue_to_pr_with_slot(self, job_id: str) -> None:
        with self._run_slots:
            if self.settings.local_repo_path:
                with self._local_workspace_lock:
                    self._continue_to_pr(job_id)
            else:
                self._continue_to_pr(job_id)

    def _continue_to_pr(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        params = job["parameters"]
        stored = job["result"] or {}
        def log(message: str) -> None:
            self.log(job_id, message)
        result: dict = {}
        review: dict = {}
        started_at = time.time()
        self.store.update(job_id, status="running", stage="Resuming to PR")
        try:
            issue_ref = parse_issue_url(params["issue_url"])
            base_branch = stored["base_branch"]
            branch_name = stored["branch_name"]
            artifact_dir = self.settings.workspace_root / job_id
            github = GitHubOps(self.settings.command_timeout_seconds, log)
            issue = github.get_issue(issue_ref)
            test_plan = self._get_cached_test_plan(issue_ref)

            repo_names = stored.get("changed_repos") or [issue_ref.repo]
            changed_repos: dict[str, Path] = {}
            for repo_name in repo_names:
                if self.settings.local_repo_path:
                    candidate = self.settings.local_repo_path / repo_name
                    repo_dir = candidate if (candidate / ".git").exists() else self.settings.local_repo_path
                else:
                    repo_dir = artifact_dir / "repo" if repo_name == issue_ref.repo else artifact_dir / repo_name / "repo"
                if not (repo_dir / ".git").exists():
                    raise WorkflowError(f"Could not find the previously cloned repository for {repo_name} at {repo_dir}.")
                changed_repos[repo_name] = repo_dir

            result = {
                "commit_message": stored["commit_message"], "pr_title": stored["pr_title"],
                "confidence": stored["confidence"], "summary": stored["summary"],
                "root_cause": stored["root_cause"],
                "evidence": stored.get("evidence") or [],
                "files_changed": stored.get("files_changed") or [],
                "tests_run": stored.get("tests_run") or [],
                "unresolved_risks": stored.get("unresolved_risks") or [],
                "completion_requirements": stored.get("completion_requirements") or [],
                "pr_notes": stored.get("pr_notes") or "",
            }
            review = stored["review"]
            reviews = stored.get("reviews") or {name: review for name in changed_repos}

            self._finish_pr(
                job_id, params, log, issue_ref, issue, base_branch, branch_name,
                changed_repos, result, review, reviews, test_plan, github, artifact_dir, started_at,
            )
        except Exception as exc:  # noqa: BLE001 - workflow boundary must record every failure
            current = self.store.get(job_id)
            if not current or current.get("status") != "stopped":
                failed_stage = "Fix/retest stopped — PR unchanged" if is_qa_fix else "Failed"
                self.store.update(job_id, status="failed", stage=failed_stage, error=str(exc))
            log(f"ERROR: {exc}")

    def _finish_pr(
        self, job_id: str, params: dict, log, issue_ref, issue: dict, base_branch: str,
        branch_name: str, changed_repos: dict[str, Path], result: dict, review: dict,
        reviews: dict, test_plan: dict | None, github: GitHubOps, artifact_dir: Path, started_at: float,
        existing_prs: dict[str, dict] | None = None,
    ) -> None:
        updating_existing = existing_prs is not None
        try:
            publish_confidence = float(result.get("confidence", 0))
        except (TypeError, ValueError) as exc:
            raise WorkflowError("PR publication requires a valid confidence score.") from exc
        if publish_confidence < self.settings.minimum_confidence:
            raise WorkflowError(
                f"PR publication blocked: confidence {publish_confidence:.0%} is below "
                f"{self.settings.minimum_confidence:.0%}. Model escalation must complete first."
            )
        push_stage = "100% passed — updating existing PR branch" if updating_existing else "Committing and pushing"
        self.approve_stage(job_id, push_stage)
        self.stage(job_id, push_stage)
        issue_link = f"{issue_ref.owner}/{issue_ref.repo}#{issue_ref.number}"
        repo_refs = {
            name: type(issue_ref)(owner=issue_ref.owner, repo=name, number=issue_ref.number)
            for name in changed_repos
        }
        repo_metadata: dict[str, dict] = {}
        primary_default = issue.get("repository_default_branch")
        if issue_ref.repo in repo_refs and primary_default:
            repo_metadata[issue_ref.repo] = {"default_branch": primary_default}
        metadata_names = [name for name in repo_refs if name not in repo_metadata]
        if metadata_names:
            with ThreadPoolExecutor(max_workers=min(3, len(metadata_names))) as executor:
                metadata_futures = {
                    executor.submit(github.get_repository, repo_refs[name]): name
                    for name in metadata_names
                }
                for future in as_completed(metadata_futures):
                    repo_metadata[metadata_futures[future]] = future.result()

        def publish_repository(repo_name: str, current_repo_dir: Path) -> None:
            log(f"Committing repository: {repo_name}")
            target_branch = (
                existing_prs[repo_name]["headRefName"] if existing_prs is not None else branch_name
            )
            github.commit_and_push(
                current_repo_dir,
                target_branch,
                str(result["commit_message"]),
                repo_refs[repo_name].full_name,
            )
        if len(changed_repos) == 1:
            publish_repository(*next(iter(changed_repos.items())))
        else:
            with ThreadPoolExecutor(max_workers=min(3, len(changed_repos))) as executor:
                publish_futures = [
                    executor.submit(publish_repository, name, path)
                    for name, path in changed_repos.items()
                ]
                for future in publish_futures:
                    future.result()

        summaries: dict[str, tuple[list[str], int, int]] = {}
        with ThreadPoolExecutor(max_workers=min(3, len(changed_repos))) as executor:
            summary_futures = {
                executor.submit(github.diff_summary, path, base_branch): name
                for name, path in changed_repos.items()
            }
            for future in as_completed(summary_futures):
                summaries[summary_futures[future]] = future.result()

        if updating_existing:
            self.stage(job_id, "Existing PR branch updated")
            pr_urls = {name: existing_prs[name]["url"] for name in changed_repos}
            log("Updated existing pull request(s): " + ", ".join(pr_urls.values()))
        else:
            self.approve_stage(job_id, "Creating pull request")
            self.stage(job_id, "Creating pull request")
            pr_urls: dict[str, str] = {}
            for repo_name, current_repo_dir in changed_repos.items():
                repo_artifact_dir = artifact_dir / repo_name
                repo_artifact_dir.mkdir(parents=True, exist_ok=True)
                default_branch = repo_metadata[repo_name].get("default_branch")
                # A closing keyword ("Fixes") is what makes the PR appear in the
                # issue's Development section. GitHub only auto-closes the issue when
                # the PR merges into the default branch, so this is safe on other
                # branches regardless of close_issue_on_merge.
                relation = "Fixes" if repo_name == issue_ref.repo else "Relates to"
                repo_review = reviews[repo_name]
                repo_result = {
                    **result,
                    "files_changed": summaries[repo_name][0],
                    "tests_run": [
                        item for item in self._dict_items(result.get("tests_run"))
                        if not item.get("repository") or item.get("repository") == repo_name
                    ],
                }
                pr_body = self._build_pr_body(
                    repo_result,
                    format_review_markdown(repo_review),
                    relation,
                    issue_link,
                    base_branch,
                    default_branch,
                )
                title = str(result["pr_title"])
                if len(changed_repos) > 1:
                    title = f"{title} ({repo_name})"
                pr_urls[repo_name] = github.create_pr(
                    repo_refs[repo_name],
                    current_repo_dir,
                    base_branch,
                    branch_name,
                    title,
                    pr_body,
                    repo_artifact_dir,
                )

            notification_thread = threading.Thread(
                target=self._send_pr_notification,
                args=(issue, result, pr_urls, log),
                daemon=False,
            )
            notification_thread.start()
            notification_thread.join(timeout=15)

        pr_url = pr_urls.get(issue_ref.repo) or next(iter(pr_urls.values()))

        partial_result = {
            "ticket_url": issue["html_url"],
            "repository": issue_ref.full_name,
            "issue_number": issue_ref.number,
            "base_branch": base_branch,
            "branch_name": branch_name,
            "pr_url": pr_url,
            "pr_urls": pr_urls,
            "confidence": result["confidence"],
            "summary": result["summary"],
            "root_cause": result["root_cause"],
            "tests_run": result.get("tests_run") or [],
            "overall": "passed" if updating_existing else None,
            "review": review,
        }
        self.store.update(job_id, result_json=partial_result)

        self.stage(job_id, "Posting code review")
        def post_repository_review(repo_name: str, current_pr_url: str) -> None:
            repo_artifact_dir = artifact_dir / repo_name
            github.post_review(
                repo_refs[repo_name],
                current_pr_url,
                reviews[repo_name],
                format_review_markdown(reviews[repo_name]),
                repo_artifact_dir,
            )
        if len(pr_urls) == 1:
            post_repository_review(*next(iter(pr_urls.items())))
        else:
            with ThreadPoolExecutor(max_workers=min(3, len(pr_urls))) as executor:
                review_futures = [
                    executor.submit(post_repository_review, name, url)
                    for name, url in pr_urls.items()
                ]
                for future in review_futures:
                    future.result()

        self.stage(job_id, "Linking original ticket")
        github.comment_on_issue(
            issue_ref,
            self._build_ticket_pr_comment(
                result,
                review,
                issue_ref.number,
                base_branch,
                branch_name,
                pr_urls,
                test_plan,
                round(time.time() - started_at),
            ),
            artifact_dir,
        )

        github_login = params.get("github_login")
        if github_login:
            github.assign_issue(issue_ref, github_login)
            for repo_name, current_pr_url in pr_urls.items():
                github.assign_pr(repo_refs[repo_name], current_pr_url, github_login)

        for repo_name, current_pr_url in pr_urls.items():
            changed_paths = summaries[repo_name][0]
            if any(is_schema_file(path) for path in changed_paths):
                github.assign_pr(repo_refs[repo_name], current_pr_url, "ChrisFordPVF")
                log(f"Assigned @ChrisFordPVF because {repo_name} contains a schema or SQL change.")

        additions = sum(summary[1] for summary in summaries.values())
        deletions = sum(summary[2] for summary in summaries.values())

        final_result = {
            "ticket_url": issue["html_url"],
            "repository": issue_ref.full_name,
            "issue_number": issue_ref.number,
            "base_branch": base_branch,
            "branch_name": branch_name,
            "pr_url": pr_url,
            "pr_urls": pr_urls,
            "confidence": result["confidence"],
            "summary": result["summary"],
            "root_cause": result["root_cause"],
            "tests_run": result.get("tests_run") or [],
            "overall": "passed" if updating_existing else None,
            "review": review,
            "assignee": github_login,
            "additions": additions,
            "deletions": deletions,
            "duration_seconds": round(time.time() - started_at),
        }
        final_stage = "Existing PR updated — 100% pass" if updating_existing else "Completed"
        self.store.update(job_id, status="completed", stage=final_stage, result_json=final_result)
        target = max(60, int(getattr(self.settings, "full_delivery_target_seconds", 180)))
        if final_result["duration_seconds"] > target:
            log(
                f"Performance target exceeded by {final_result['duration_seconds'] - target}s; "
                "delivery continued because timing targets do not override safety or completion."
            )
        log("Completed: " + ", ".join(pr_urls.values()))

    def _send_pr_notification(self, issue: dict, result: dict, pr_urls: dict[str, str], log) -> None:
        """Show a best-effort local Windows notification after PR creation."""
        title = f"MergeQuest: PR created #{issue.get('number', '')}".strip()
        links = "\n".join(f"- {repository}: {url}" for repository, url in pr_urls.items())
        confidence = float(result.get("confidence", 0))
        body = f"{result.get('summary', '')} | Confidence {confidence:.0%} | {links}"
        self._send_windows_notification(title, body, log, launch_url=issue.get("html_url"))

    @staticmethod
    def _send_windows_notification(title: str, body: str, log, launch_url: str | None = None) -> None:
        """Show a best-effort local Windows toast notification; no-op on other platforms.

        Uses the WinRT toast API directly rather than msg.exe: msg.exe depends on the
        Remote Desktop "TermService", which is stopped/manual on most desktop installs,
        so it reports success while showing nothing.
        """
        if os.name != "nt" and not shutil.which("powershell.exe"):
            log("Windows notification skipped: this host is not Windows.")
            return
        try:
            escaped_title = title.replace("'", "''")
            escaped_body = body.replace("'", "''")
            escaped_launch_url = str(launch_url or "").replace("'", "''")
            launch_script = ""
            if escaped_launch_url:
                launch_script = (
                    f"$t.DocumentElement.SetAttribute('launch', '{escaped_launch_url}');"
                    "$actions = $t.CreateElement('actions');"
                    "$action = $t.CreateElement('action');"
                    "$action.SetAttribute('content', 'Open ticket');"
                    "$action.SetAttribute('activationType', 'protocol');"
                    f"$action.SetAttribute('arguments', '{escaped_launch_url}');"
                    "$actions.AppendChild($action) | Out-Null;"
                    "$t.DocumentElement.AppendChild($actions) | Out-Null;"
                )
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] | Out-Null;"
                "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$x = $t.GetElementsByTagName('text');"
                f"$x.Item(0).AppendChild($t.CreateTextNode('{escaped_title}')) | Out-Null;"
                f"$x.Item(1).AppendChild($t.CreateTextNode('{escaped_body}')) | Out-Null;"
                f"{launch_script}"
                "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('MergeQuest');"
                "$notifier.Show([Windows.UI.Notifications.ToastNotification]::new($t));"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                check=False, timeout=10, capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise subprocess.SubprocessError(result.stderr.strip() or f"exit code {result.returncode}")
            log("Windows notification sent.")
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"Windows notification failed: {exc}")

    def _comment_on_failure(
        self, job_id: str, params: dict, exc: Exception, job: dict | None,
        result: dict, review: dict, log,
    ) -> None:
        """Best-effort failure reporting; reporting must never hide the original error."""
        try:
            issue_ref = parse_issue_url(params["issue_url"])
            artifact_dir = self.settings.workspace_root / job_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            github = GitHubOps(self.settings.command_timeout_seconds, log)
            failed_stage = (job or {}).get("stage") or "Unknown stage"
            github.comment_on_issue(
                issue_ref,
                self._failure_comment(failed_stage, str(exc), result, review),
                artifact_dir,
            )
            log("Posted failure guidance on the original ticket.")
        except Exception as comment_exc:  # noqa: BLE001 - best-effort notification
            log(f"Could not post failure guidance on the ticket: {comment_exc}")

    @staticmethod
    def _classify_failure(error: str, result: dict, review: dict) -> tuple[str, str]:
        """Map a raw failure onto a ticket-facing diagnosis category and explanation."""
        lowered = error.lower()
        combined = " ".join(
            [lowered, str(result.get("root_cause", "")).lower()]
            + [str(risk).lower() for risk in (result.get("unresolved_risks") or [])]
            + [str(item).lower() for item in (result.get("completion_requirements") or [])]
        )
        if any(term in combined for term in ("schema", "migration", "table", " column", "database model")):
            return (
                "Missing schema or migration",
                "The ticket depends on a data model that does not exist yet. The fix cannot land "
                "until the schema (table, column, or migration) it relies on is created.",
            )
        if any(term in combined for term in ("does not exist", "not found", "no such", "missing", "undefined", "unavailable required access")):
            return (
                "Missing prerequisite",
                "Something the ticket assumes is available — an API, endpoint, service, file, or "
                "configuration — could not be found. It must be created or made reachable first.",
            )
        if "review still has blocking" in lowered or (review.get("verdict") == "BLOCK"):
            return (
                "Blocking review findings",
                "A fix was implemented, but the independent code review found merge-blocking defects "
                "that repair cycles could not resolve.",
            )
        if any(term in lowered for term in ("failed validation", "validation", "reported failed", "no passing check")):
            return (
                "Validation failure",
                "A change was made, but the required checks did not pass. The failing checks below "
                "describe exactly what the ticket still needs.",
            )
        if "confidence" in lowered:
            return (
                "Low confidence",
                "The agent produced a change but could not gather enough evidence to be confident it "
                "is correct. The ticket likely needs clearer reproduction steps or expected behavior.",
            )
        if "unsafe to submit" in lowered or "unresolved risks" in lowered:
            return (
                "Agent declined to submit",
                "The agent judged the change unsafe to raise as a PR. The specifics below explain "
                "what makes it risky in the context of this ticket.",
            )
        if any(term in lowered for term in ("auth", "permission", "token", "credential", "forbidden", "401", "403")):
            return (
                "Access problem",
                "The workflow could not authenticate or lacked permission for a required GitHub or "
                "service operation. No conclusion about the ticket itself should be drawn from this run.",
            )
        if any(term in lowered for term in ("not installed", "on path", "timed out", "executable")):
            return (
                "Environment problem",
                "The automation environment is missing a required tool or timed out. This is an "
                "infrastructure issue, not a problem with the ticket.",
            )
        return (
            "Unclassified failure",
            "The run stopped for a reason that does not match a known pattern; the raw error below "
            "has the details.",
        )

    @staticmethod
    def _dict_items(items) -> list[dict]:
        """Keep agent-provided arrays safe when a CLI returns scalar items."""
        return [item for item in (items or []) if isinstance(item, dict)]

    def _destructive_test_verification(
        self, repos: list[Path], prepared_prs: dict[str, dict],
        original_refs: dict[Path, str], result: dict, log,
    ) -> None:
        """Verify tests fail without the fix; proves tests validate the fix."""
        if not prepared_prs:
            log("No linked PRs to verify; skipping destructive test validation.")
            return
        test_commands = [
            item.get("command") for item in self._dict_items(result.get("tests_run"))
            if str(item.get("result", "")).lower() == "passed" and item.get("command")
        ]
        if not test_commands:
            log("No passing test commands to destructively validate.")
            return
        stashes: dict[Path, str] = {}
        workspace_dir = repos[0].parent if repos else None
        try:
            log(f"Destructive verification: removing fix and re-running {len(test_commands)} test(s)")
            # Stash all changes first
            for repo_dir in repos:
                if repo_dir not in original_refs:
                    continue
                run_command(
                    ["git", "stash", "push", "-m", "qa-verify"],
                    cwd=repo_dir, timeout=self.settings.command_timeout_seconds, log=log, check=True,
                )
                stashes[repo_dir] = "stashed"
            # Then revert all to original refs
            for repo_dir in repos:
                if repo_dir not in original_refs:
                    continue
                run_command(
                    ["git", "checkout", original_refs[repo_dir]],
                    cwd=repo_dir, timeout=self.settings.command_timeout_seconds, log=log, check=True,
                )
                log(f"Reverted {repo_dir.name} to `{original_refs[repo_dir]}`")
            # Run tests on unfixed code
            retest_failed_count = 0
            for cmd in test_commands:
                test_result = run_command(
                    cmd, cwd=workspace_dir,
                    timeout=self.settings.command_timeout_seconds, log=log, check=False,
                )
                if test_result.returncode == 0:
                    log(
                        f"⚠ Test still passed without fix: {cmd}\n"
                        "  The test may not validate the specific fix."
                    )
                    retest_failed_count += 1
                else:
                    log(f"✓ Test correctly failed without fix: {cmd}")
            if retest_failed_count > 0:
                raise WorkflowError(
                    f"{retest_failed_count} test(s) passed without the fix; not validating it. "
                    "Escalating to a more capable model for better test generation."
                )
        finally:
            # Restore all changes in reverse order
            for repo_dir in reversed(list(stashes.keys())):
                try:
                    run_command(
                        ["git", "stash", "pop"],
                        cwd=repo_dir, timeout=self.settings.command_timeout_seconds, log=log, check=True,
                    )
                    log(f"Restored fix to {repo_dir.name}")
                except (WorkflowError, subprocess.CalledProcessError) as exc:
                    log(f"Error: could not restore {repo_dir.name} after destructive verification: {exc}")
                    raise WorkflowError(
                        f"Could not restore fix in {repo_dir.name} after destructive verification. "
                        "Repository may be in an inconsistent state; manually verify."
                    )

    @staticmethod
    def _is_manual_ui_skip(item: dict) -> bool:
        """Exclude unavailable manual UI checks from automated QA evidence."""
        if str(item.get("result", "")).lower() not in {"skipped", "not-run"}:
            return False
        text = f"{item.get('command', '')} {item.get('notes', '')}".lower()
        markers = (
            "ui interaction", "manual ui", "visual verification", "visual persistence",
            "windows desktop session", "running app instance", "browser interaction",
            "browser session", "desktop application", "open/close animation",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_autonomous_frontend_test(item: dict) -> bool:
        """Reject browser/UI automation; the scanner is deliberately non-visual."""
        text = f"{item.get('command', '')} {item.get('notes', '')}".lower()
        markers = (
            "playwright", "cucumber", "cypress", "selenium", "puppeteer",
            "webdriver", "browser automation", "visual regression",
            "screenshot comparison", "desktop interaction",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _qa_outcome(statuses: list[str]) -> str:
        """Skipped checks are informational when every executed check passes."""
        normalized = [str(status).lower() for status in statuses]
        if "failed" in normalized:
            return "failed"
        if "passed" in normalized and all(status in {"passed", "skipped", "not-run"} for status in normalized):
            return "passed"
        return "incomplete"

    @staticmethod
    def _failure_comment(failed_stage: str, error: str, result: dict, review: dict) -> str:
        category, explanation = WorkflowRunner._classify_failure(error, result, review)

        requirements = [str(item) for item in (result.get("completion_requirements") or []) if str(item).strip()]
        if not requirements:
            requirements.extend(str(item) for item in (result.get("unresolved_risks") or []) if str(item).strip())
        for test in WorkflowRunner._dict_items(result.get("tests_run")):
            if str(test.get("result", "")).lower() == "failed":
                requirements.append(
                    f"Fix `{test.get('command', 'the failing validation')}`: {test.get('notes') or 'the check did not pass.'}"
                )
        for finding in WorkflowRunner._dict_items(review.get("findings")):
            if str(finding.get("severity", "")).upper() in {"HIGH", "CRITICAL"}:
                requirements.append(f"{finding.get('title', 'Resolve review blocker')}: {finding.get('body', '')}".strip())
        if not requirements:
            requirements.append(error)
        checklist = "\n".join(f"- [ ] {item}" for item in dict.fromkeys(requirements))

        sections: list[str] = [
            "## ⛔ Automated run blocked — " + category,
            f"The automated work on this ticket stopped during **{failed_stage}**.",
            f"**Diagnosis:** {explanation}",
        ]
        if result.get("root_cause"):
            sections.append(f"**Technical context:** {result['root_cause']}")
        if result.get("summary"):
            sections.append(f"**What was attempted:** {result['summary']}")

        evidence = [str(item) for item in (result.get("evidence") or []) if str(item).strip()]
        if evidence:
            sections.append("**Evidence gathered:**\n" + "\n".join(f"- `{item}`" for item in evidence))

        files = [str(item) for item in (result.get("files_changed") or []) if str(item).strip()]
        if files:
            sections.append("**Files touched before the run stopped:**\n" + "\n".join(f"- `{item}`" for item in files))

        sections.append("### Required to complete this ticket\n" + checklist)

        tests = WorkflowRunner._dict_items(result.get("tests_run"))
        if tests:
            rows = "\n".join(
                f"| `{item.get('command', '?')}` | {item.get('result', 'unknown')} | {item.get('notes') or ''} |"
                for item in tests
            )
            sections.append("### Validation results\n| Check | Result | Notes |\n| --- | --- | --- |\n" + rows)

        sections.append(f"<details><summary>Raw job failure</summary>\n\n`{error}`\n\n</details>")
        sections.append(
            "Once the items above are addressed, this ticket can be run through investigation, "
            "validation, and review again."
        )
        return "\n\n".join(sections)

    @staticmethod
    def _no_change_comment(result: dict) -> str:
        sections: list[str] = [
            "## ℹ️ No code change needed in this repository",
            "The automated agent investigated this ticket and concluded, with evidence, that no "
            "source change is required here.",
        ]
        if result.get("root_cause"):
            sections.append(f"**Root cause:** {result['root_cause']}")
        if result.get("summary"):
            sections.append(f"**Investigation summary:** {result['summary']}")

        requirements = [str(item) for item in (result.get("completion_requirements") or []) if str(item).strip()]
        if requirements:
            sections.append(
                "### Still required to fully close this ticket\n"
                + "\n".join(f"- [ ] {item}" for item in requirements)
            )

        risks = [str(item) for item in (result.get("unresolved_risks") or []) if str(item).strip()]
        if risks:
            sections.append("**Unresolved risks:**\n" + "\n".join(f"- {item}" for item in risks))

        if result.get("pr_notes"):
            sections.append(f"**Notes:** {result['pr_notes']}")

        sections.append(f"Confidence: {round(float(result.get('confidence') or 0) * 100)}%")
        return "\n\n".join(sections)

    def _get_cached_test_plan(self, issue_ref) -> dict | None:
        """Read optional Recon Protocol notes without launching another agent."""
        key = f"{issue_ref.full_name}#{issue_ref.number}"
        return self.store.get_ticket_test(key)

    @staticmethod
    def _format_test_plan_markdown(test_plan: dict | None) -> str:
        if not test_plan or not (test_plan.get("repro_steps") or test_plan.get("pass_steps")):
            return ""
        sections = ["\n\n## Test plan"]
        if test_plan.get("repro_steps"):
            sections.append("**Steps to reproduce the original issue:**\n" + "\n".join(f"- [ ] {step}" for step in test_plan["repro_steps"]))
        if test_plan.get("pass_steps"):
            sections.append("**Steps to verify the fix:**\n" + "\n".join(f"- [ ] {step}" for step in test_plan["pass_steps"]))
        return "\n\n".join(sections)

    @staticmethod
    def _copy_review_untracked_files(repo_dir: Path, review_dir: Path) -> None:
        """Copy source-relevant untracked files into an isolated review worktree."""
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=repo_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowError("Could not enumerate untracked files for isolated review.")
        for relative_text in (item for item in result.stdout.split("\0") if item):
            relative = Path(relative_text)
            if relative.parts and relative.parts[0] == ".ticket-agent":
                continue
            source = repo_dir / relative
            target = review_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(source.readlink())
            elif source.is_file():
                shutil.copy2(source, target)

    def _review_changed_repositories(
        self,
        job_id: str,
        repositories: dict[str, Path],
        issue: dict,
        implementation: dict,
        base_branch: str,
        command: str,
        log,
        delivery_started_at: float | None = None,
    ) -> dict[str, dict]:
        """Review independent repository diffs concurrently to shorten the critical path."""
        self.approve_stage(job_id, "Reviewing the change")
        self.stage(job_id, "Reviewing the change")
        repository_label = "repository" if len(repositories) == 1 else "repositories"
        log(
            f"Black Ice audit started for {len(repositories)} {repository_label}"
            + (" in parallel." if len(repositories) > 1 else ".")
        )
        # Each reviewer can inspect its own isolated checkout directly. Supplying
        # that same full diff in the prompt duplicated thousands of tokens. Only
        # multi-repository reviews need bounded *sibling* context.
        coordinated_context = (
            self._coordinated_review_context(repositories)
            if len(repositories) > 1 else {}
        )
        reviews: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=min(3, len(repositories))) as executor:
            pending = {
                executor.submit(
                    self._review,
                    job_id,
                    path,
                    issue,
                    base_branch,
                    command,
                    log,
                    {
                        **implementation,
                        "coordinated_repository_changes": {
                            sibling: context
                            for sibling, context in coordinated_context.items()
                            if sibling != name
                        },
                    },
                    False,
                    delivery_started_at,
                ): name
                for name, path in repositories.items()
            }
            for future in as_completed(pending):
                name = pending[future]
                reviews[name] = future.result()
                log(f"Black Ice audit complete: {name}.")
        return {name: reviews[name] for name in repositories}

    @staticmethod
    def _coordinated_review_context(repositories: dict[str, Path]) -> dict[str, dict]:
        """Capture bounded sibling diffs so isolated reviewers can assess one coordinated fix."""
        context: dict[str, dict] = {}
        # Reviewers inspect their own checkout directly; sibling context only
        # needs enough contract/schema detail to validate cross-repo alignment.
        per_repository_limit = 6_000
        for name, repo_dir in repositories.items():
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=repo_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            diff = subprocess.run(
                ["git", "diff", "--no-ext-diff", "--unified=12", "HEAD"],
                cwd=repo_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if status.returncode != 0 or diff.returncode != 0:
                raise WorkflowError(f"Could not prepare coordinated review context for {name}.")
            evidence = diff.stdout
            remaining = max(0, per_repository_limit - len(evidence))
            if remaining:
                for status_line in status.stdout.splitlines():
                    if not status_line.startswith("?? "):
                        continue
                    relative = status_line[3:].strip()
                    source = (repo_dir / relative).resolve()
                    try:
                        source.relative_to(repo_dir.resolve())
                    except ValueError:
                        continue
                    if not source.is_file():
                        continue
                    try:
                        content = source.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    addition = f"\n\n--- untracked file: {relative} ---\n{content}"
                    evidence += addition[:remaining]
                    remaining = max(0, per_repository_limit - len(evidence))
                    if not remaining:
                        break
            truncated = len(evidence) > per_repository_limit
            context[name] = {
                "working_tree": status.stdout.splitlines(),
                "diff": evidence[:per_repository_limit],
                "diff_truncated": truncated,
            }
        return context

    def _review(
        self,
        job_id: str,
        repo_dir: Path,
        issue: dict,
        base_branch: str,
        command: str,
        log,
        implementation: dict | None = None,
        manage_stage: bool = True,
        delivery_started_at: float | None = None,
    ) -> dict:
        if manage_stage:
            self.approve_stage(job_id, "Reviewing the change")
            self.stage(job_id, "Reviewing the change")
        review_path = repo_dir / ".ticket-agent" / "review.json"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.unlink(missing_ok=True)
        prompt = review_prompt(issue, base_branch, implementation)
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.with_name("review-prompt.md").write_text(prompt, encoding="utf-8")

        temporary_path = Path(tempfile.mkdtemp(prefix="ticket-review-", dir=self.settings.workspace_root))
        temporary_path.rmdir()  # `git worktree add` requires a path that does not exist.
        worktree_added = False
        try:
            run_command(
                ["git", "worktree", "add", "--detach", str(temporary_path), "HEAD"],
                cwd=repo_dir,
                timeout=self.settings.command_timeout_seconds,
                log=log,
            )
            worktree_added = True

            patch = subprocess.run(
                ["git", "diff", "--binary", "--full-index", "HEAD"],
                cwd=repo_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if patch.returncode != 0:
                raise WorkflowError("Could not prepare the implementation diff for isolated review.")
            if patch.stdout:
                run_command(
                    ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
                    cwd=temporary_path,
                    stdin_text=patch.stdout,
                    timeout=self.settings.command_timeout_seconds,
                    log=log,
                )
            self._copy_review_untracked_files(repo_dir, temporary_path)

            isolated_review_path = temporary_path / ".ticket-agent" / "review.json"
            isolated_review_path.parent.mkdir(parents=True, exist_ok=True)
            prompt = review_prompt(
                issue, base_branch, implementation,
                output_path=str(isolated_review_path),
            )
            isolated_review_path.with_name("review-prompt.md").write_text(prompt, encoding="utf-8")
            before = working_tree_fingerprint(temporary_path)
            review_command = command
            review_attempt = 0
            while True:
                review_attempt += 1
                isolated_review_path.unlink(missing_ok=True)
                try:
                    run_configured_command(
                        review_command,
                        cwd=temporary_path,
                        prompt=prompt,
                        timeout=self._delivery_timeout(
                            self._review_timeout(review_command),
                            delivery_started_at,
                            floor_seconds=20,
                            reserve_seconds=self._full_delivery_reserves()[1],
                        ),
                        log=log,
                        should_abort=lambda: self._should_abort(job_id),
                    )
                    break
                except WorkflowError as exc:
                    promoted_command, previous_model, promoted_model = self._escalate_command(review_command)
                    if review_attempt >= 2 or not promoted_model:
                        raise
                    review_command = promoted_command
                    log(
                        f"Review model escalation: {previous_model} -> {promoted_model} "
                        f"after reviewer gate failure: {exc}"
                    )
            after = working_tree_fingerprint(temporary_path)
            if before != after:
                log("Reviewer attempted source edits in its isolated checkout; those edits were discarded.")

            if not isolated_review_path.is_file():
                review = {
                    "verdict": "BLOCK",
                    "summary": "The independent reviewer completed without producing its required result.",
                    "findings": [{
                        "severity": "HIGH",
                        "title": "Independent review result was not produced",
                        "body": (
                            "The reviewer did not write .ticket-agent/review.json in the isolated "
                            "target-repository checkout. Retry the review stage; no implementation "
                            "changes were discarded."
                        ),
                        "path": "",
                        "line": 1,
                        "side": "RIGHT",
                    }],
                }
                isolated_review_path.write_text(json.dumps(review), encoding="utf-8")
                log("Reviewer output was missing; recorded a structured blocking review result.")
            else:
                review = load_json(isolated_review_path)
            ensure_keys(review, ["verdict", "summary", "findings"], "Review result")
            if not isinstance(review.get("findings"), list):
                raise WorkflowError("Review findings must be a JSON array.")
            shutil.copy2(isolated_review_path, review_path)
            return review
        finally:
            if worktree_added:
                run_command(
                    ["git", "worktree", "remove", "--force", str(temporary_path)],
                    cwd=repo_dir,
                    timeout=self.settings.command_timeout_seconds,
                    log=log,
                    check=False,
                )
            shutil.rmtree(temporary_path, ignore_errors=True)

    def _review_timeout(self, command: str) -> int | None:
        """Return the configured reviewer timeout; 0 disables model time limits."""
        del command
        value = int(getattr(self.settings, "review_timeout_seconds", 0))
        return value if value > 0 else None

    def _open_editor(self, repo_dir: Path, log) -> None:
        command = shlex.split(self.settings.editor_command)
        if not command or shutil.which(command[0]) is None:
            log("Configured editor opener is unavailable; continuing with the automated workflow.")
            return
        try:
            subprocess.Popen([*command, str(repo_dir)], cwd=repo_dir)
            log(f"Opened workspace in editor: {repo_dir}")
        except OSError as exc:
            log(f"Could not open workspace in editor: {exc}")

    def _run_agent_gated(
        self, job_id: str, agent_command: str, workspace_dir: Path,
        result_path: Path, issue: dict, prompt: str, log,
        *, require_all_tests_passed: bool = False,
        auto_escalate_model: bool = True,
        delivery_started_at: float | None = None,
    ) -> dict:
        """Run the coding agent, and on a gate failure (low confidence, unsafe,
        unresolved risks, failed checks) loop with a repair prompt until the
        result passes the confidence gate, up to max_gate_attempts."""
        attempt = 0
        attempts_on_model = 0
        repeated_failures = 0
        last_failure = ""
        while True:
            attempt += 1
            attempts_on_model += 1
            self._publish_prompt_tokens(job_id, prompt)
            result_path.unlink(missing_ok=True)
            try:
                run_configured_command(
                    agent_command,
                    cwd=workspace_dir,
                    prompt=prompt,
                    timeout=self._delivery_timeout(
                        getattr(self.settings, "agent_pass_timeout_seconds", self.settings.command_timeout_seconds),
                        delivery_started_at,
                        floor_seconds=20,
                        reserve_seconds=sum(self._full_delivery_reserves()),
                    ),
                    log=log,
                    should_abort=lambda: self._should_abort(job_id),
                )
                result = load_json(result_path)
                result.setdefault("prompt_tokens", max(1, round(len(prompt) / 4)))
                ensure_keys(
                    result,
                    ["safe_to_pr", "confidence", "summary", "root_cause", "tests_run", "unresolved_risks", "commit_message", "pr_title"],
                    "Agent result",
                )
                self._log_agent_result(result, log)
                self.publish_agent_confidence(job_id, result.get("confidence"))
                self._gate_agent_result(result, require_all_tests_passed=require_all_tests_passed)
                return result
            except WorkflowError as exc:
                job = self.store.get(job_id)
                if not job or job["status"] == "stopped":
                    raise
                # A stale Codex cache can prevent the nested provider from
                # starting at all. Recover this provider-specific failure by
                # handing the same prompt to the configured Claude runner.
                # This is deliberately one-way and one-time per gate attempt;
                # ordinary coding failures still use the normal retry path.
                failure_text = str(exc).lower()
                if (
                    "codex" in agent_command.lower()
                    and "models cache" in failure_text
                    and "supports_reasoning_summaries" in failure_text
                    and self.settings.claude_command
                ):
                    agent_command = self.settings.claude_command
                    attempts_on_model = 0
                    log("Codex model cache is incompatible; retrying with the configured Claude runner.")
                    continue
                # The installed Codex CLI can lag behind the model catalog. Retrying
                # the same unsupported model just repeats the same rejection, so
                # escalate immediately instead of burning gate attempts on it.
                if "requires a newer version of codex" in failure_text and auto_escalate_model:
                    promoted_command, previous_model, promoted_model = self._escalate_command(agent_command)
                    if promoted_model:
                        agent_command = promoted_command
                        attempts_on_model = 0
                        log(
                            f"Model escalation: {previous_model} -> {promoted_model} "
                            f"(installed Codex CLI does not yet support {previous_model})"
                        )
                        continue
                if attempt >= self.settings.max_gate_attempts:
                    raise WorkflowError(
                        f"Gate still failing after {attempt} attempts: {exc}"
                    ) from exc
                self._delivery_timeout(
                    getattr(self.settings, "agent_pass_timeout_seconds", self.settings.command_timeout_seconds),
                    delivery_started_at,
                    floor_seconds=20,
                    reserve_seconds=sum(self._full_delivery_reserves()),
                )
                signature = str(exc).strip().lower()
                repeated_failures = repeated_failures + 1 if signature == last_failure else 1
                last_failure = signature
                # Confidence gets its full five attempts; identical infrastructure
                # failures can escalate early because another model cannot repair them.
                should_escalate = attempts_on_model >= 5 or (
                    repeated_failures >= 2 and "command failed" in signature
                )
                if should_escalate and auto_escalate_model:
                    promoted_command, previous_model, promoted_model = self._escalate_command(agent_command)
                    if promoted_model:
                        agent_command = promoted_command
                        attempts_on_model = 0
                        log(
                            f"Model escalation: {previous_model} -> {promoted_model} "
                            f"after {'repeated failure' if repeated_failures >= 2 and attempts_on_model < 5 else '5 confidence attempts'}: {exc}"
                        )
                log(
                    f"Gate failed on attempt {attempt}/{self.settings.max_gate_attempts}: {exc} "
                    f"Retrying until confidence >= {self.settings.minimum_confidence:.2f} and the change is safe to PR."
                )
                prompt = confidence_gate_prompt(
                    issue,
                    str(exc),
                    str(result_path),
                    run_validation=require_all_tests_passed,
                    minimum_confidence=self.settings.minimum_confidence,
                )

    def _full_delivery_reserves(self) -> tuple[int, int]:
        """Keep only publication reserve; model stages no longer reserve time."""
        target = max(60, int(getattr(self.settings, "full_delivery_target_seconds", 180)))
        review = 0
        publish = min(
            max(0, int(getattr(self.settings, "full_delivery_publish_reserve_seconds", 30))),
            max(10, target // 6),
        )
        return review, publish

    def _delivery_timeout(
        self,
        configured_timeout: int | None,
        delivery_started_at: float | None,
        *,
        floor_seconds: int,
        reserve_seconds: int = 0,
    ) -> int | None:
        """Return the phase timeout; 0/None disables MergeQuest model deadlines."""
        del delivery_started_at, floor_seconds, reserve_seconds
        if configured_timeout is None:
            return None
        configured = int(configured_timeout)
        return configured if configured > 0 else None

    def _gate_agent_result(self, result: dict, *, require_all_tests_passed: bool = False) -> None:
        try:
            confidence = float(result["confidence"])
        except (TypeError, ValueError) as exc:
            raise WorkflowError("Agent confidence must be a number between 0 and 1.") from exc
        if not 0 <= confidence <= 1:
            raise WorkflowError("Agent confidence must be between 0 and 1.")
        if result["safe_to_pr"] is not True:
            details = []
            if result.get("root_cause"):
                details.append(f"Root cause: {result['root_cause']}")
            risks = result.get("unresolved_risks") or []
            if risks:
                details.append("Unresolved risks: " + "; ".join(map(str, risks)))
            suffix = " " + " ".join(details) if details else ""
            raise WorkflowError(
                "The coding agent marked the change as unsafe to submit as a PR." + suffix
            )
        if confidence < self.settings.minimum_confidence:
            raise WorkflowError(
                f"Agent confidence {confidence:.2f} is below the required {self.settings.minimum_confidence:.2f}."
            )
        risks = result.get("unresolved_risks") or []
        if risks:
            raise WorkflowError("Unresolved risks remain: " + "; ".join(map(str, risks)))
        failed_tests = [
            item for item in self._dict_items(result.get("tests_run"))
            if str(item.get("result", "")).lower() == "failed"
        ]
        if failed_tests:
            raise WorkflowError("The coding agent reported failed validation checks.")
        if require_all_tests_passed:
            tests = self._dict_items(result.get("tests_run"))
            if not tests:
                raise WorkflowError("The fix/retest workflow requires at least one passing test.")
            incomplete = [
                item for item in tests
                if str(item.get("result", "")).lower() != "passed"
            ]
            if incomplete:
                raise WorkflowError(
                    "The fix/retest workflow requires a 100% pass rate; skipped or not-run checks remain."
                )

    @staticmethod
    def _classify_dirty_repositories(
        repo_dirs: dict[str, Path], branch_name: str, github: GitHubOps, log,
    ) -> tuple[set[str], list[str]]:
        resumable: set[str] = set()
        conflicts: list[str] = []
        for name, repo_dir in repo_dirs.items():
            if not github.has_changes(repo_dir):
                continue
            current_branch = github.current_branch(repo_dir)
            if current_branch == branch_name:
                resumable.add(name)
                log(f"Resuming existing ticket changes in {name} on `{branch_name}`.")
            else:
                conflicts.append(name)
        return resumable, conflicts

    @staticmethod
    def _log_agent_result(result: dict, log) -> None:
        log(f"Agent summary: {result.get('summary') or 'No summary provided.'}")
        log(f"Agent confidence: {result.get('confidence')}")
        log(f"Safe to submit as PR: {'yes' if result.get('safe_to_pr') is True else 'no'}")
        if result.get("root_cause"):
            log(f"Root cause: {result['root_cause']}")
        risks = result.get("unresolved_risks") or []
        if risks:
            log("Unresolved risks:")
            for risk in risks:
                log(f"- {risk}")

    def _run_integrity_checks(
        self,
        changed_repos: dict[str, Path],
        validation_commands: list[list[str]],
        github: GitHubOps,
        log,
    ) -> list[dict]:
        """Run post-fix checks and return the exact evidence published in the PR."""
        def validate_repository(repo_name: str, current_repo_dir: Path) -> list[dict]:
            repo_checks: list[dict] = []
            log(f"Validating repository: {repo_name}")
            github.validate_diff(current_repo_dir)
            repo_checks.append({
                "command": f"git diff --check ({repo_name})",
                "result": "passed",
                "notes": "No whitespace errors or conflict markers were found in the proposed diff.",
                "repository": repo_name,
            })
            for command in validation_commands:
                run_command(
                    command,
                    cwd=current_repo_dir,
                    timeout=self.settings.command_timeout_seconds,
                    log=log,
                )
                repo_checks.append({
                    "command": f"{shlex.join(command)} ({repo_name})",
                    "result": "passed",
                    "notes": "Configured Integrity command completed successfully.",
                    "repository": repo_name,
                })
            return repo_checks

        if len(changed_repos) == 1:
            name, path = next(iter(changed_repos.items()))
            return validate_repository(name, path)

        completed: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=min(3, len(changed_repos))) as executor:
            futures = {
                executor.submit(validate_repository, name, path): name
                for name, path in changed_repos.items()
            }
            for future in as_completed(futures):
                completed[futures[future]] = future.result()
        checks: list[dict] = []
        for name in changed_repos:
            checks.extend(completed[name])
        return checks

    @staticmethod
    def _build_pr_body(
        result: dict,
        review_markdown: str,
        relation: str,
        issue_link: str,
        base_branch: str,
        default_branch: str | None,
    ) -> str:
        tests = result.get("tests_run") or []
        test_lines = []
        for item in WorkflowRunner._dict_items(tests):
            test_lines.append(
                f"- `{item.get('command', 'not specified')}`: **{item.get('result', 'unknown')}**"
                + (f" — {item.get('notes')}" if item.get("notes") else "")
            )
        if not test_lines:
            test_lines = ["- No test commands were reported."]
        evidence = [str(item).strip() for item in (result.get("evidence") or []) if str(item).strip()]
        evidence_lines = "\n".join(f"- {item}" for item in evidence) or "- No investigation evidence was reported."
        files = [str(item).strip() for item in (result.get("files_changed") or []) if str(item).strip()]
        file_lines = "\n".join(f"- `{item}`" for item in files) or "- No changed paths were reported."
        note = ""
        if relation != "Fixes" and default_branch and base_branch != default_branch:
            note = (
                f"\n> This PR targets `{base_branch}`, not the repository default branch `{default_branch}`. "
                "It links the ticket but does not request automatic issue closure.\n"
            )
        return f"""## Summary
{result.get('summary')}

## Root cause
{result.get('root_cause')}

## Implementation
{file_lines}

## Investigation evidence
{evidence_lines}

## Validation
{chr(10).join(test_lines)}

## Confidence and risk gate
- Agent confidence: **{float(result.get('confidence', 0)):.0%}**
- Unresolved risks: **None reported**

{review_markdown}

## Ticket
{relation} {issue_link}
{note}
## Reviewer notes
{result.get('pr_notes') or 'No additional notes.'}

> Generated by Ticket PR Agent. Human approval is still required before merge.
""".strip()

    @staticmethod
    def _build_ticket_pr_comment(
        result: dict,
        review: dict,
        issue_number: int,
        base_branch: str,
        source_branch: str,
        pr_urls: dict[str, str],
        test_plan: dict | None,
        duration_seconds: int,
    ) -> str:
        def bullets(values, fallback: str) -> str:
            items = [str(value).strip() for value in (values or []) if str(value).strip()]
            return "\n".join(f"* {item}" for item in items) if items else f"* {fallback}"

        summary = str(result.get("summary") or "No summary was reported.")
        root_cause = str(result.get("root_cause") or "No root cause was reported.")
        evidence = result.get("evidence") or []
        files = result.get("files_changed") or []
        tests = WorkflowRunner._dict_items(result.get("tests_run") or [])
        findings = WorkflowRunner._dict_items(review.get("findings") or [])
        risks = result.get("unresolved_risks") or []
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
        confidence_pct = round(confidence * 100)
        overall = "High" if confidence >= .9 else "Medium" if confidence >= .7 else "Low"
        risk_level = "Low" if not risks and not findings else "Medium"
        branch_kind = source_branch.lower()
        if "refactor" in branch_kind:
            pr_type = "Refactor"
        elif branch_kind.startswith(("chore/", "maintenance/", "docs/")):
            pr_type = "Maintenance"
        elif "fix" in branch_kind or branch_kind.startswith("bug/"):
            pr_type = "Bug Fix"
        else:
            pr_type = "Feature"
        pr_links = "<br>".join(f"[{name}]({url})" for name, url in pr_urls.items())
        minutes, seconds = divmod(max(0, duration_seconds), 60)
        pr_time = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

        test_rows = []
        for item in tests:
            command = item.get("command", "not specified")
            outcome = item.get("result", "unknown")
            notes = f" — {item['notes']}" if item.get("notes") else ""
            test_rows.append(f"* `{command}`: **{outcome}**{notes}")
        test_report = "\n".join(test_rows) if test_rows else "* No automated checks were reported."
        all_passed = bool(tests) and all(str(item.get("result", "")).lower() == "passed" for item in tests)
        check = "x" if all_passed else " "

        repro = (test_plan or {}).get("repro_steps") or ["Reproduce the ticket scenario."]
        verify = (test_plan or {}).get("pass_steps") or ["Confirm the reported problem is resolved."]
        manual_steps = [*repro, *verify]
        manual = "\n".join(f"{index}. {step}" for index, step in enumerate(manual_steps, 1))
        reviewer_focus = files[:3] or ["The implementation and its alignment with the ticket requirements."]
        reviewer_list = "\n".join(f"{index}. {item}" for index, item in enumerate(reviewer_focus, 1))
        remaining = result.get("completion_requirements") or []
        notes = str(result.get("pr_notes") or "No additional reviewer notes were reported.")

        return f"""# Summary

This PR fixes/adds `{summary}`.

It resolves `{root_cause}` by `{summary}`.

## Linked Work

* **Issue:** #{issue_number}
* **Pull request:** {pr_links}
* **Base branch:** `{base_branch}`
* **Source branch:** `{source_branch}`
* **PR type:** `{pr_type}`
* **Time to PR:** `{pr_time}`

## Investigation

### Problem

`{root_cause}`

### Root Cause

`{root_cause}`

### Evidence

{bullets(evidence, "No supporting evidence was reported.")}

## Changes Made

{bullets(files, summary)}

## Scope

### Included

* {summary}

### Not Included

{bullets(remaining, "No excluded or follow-up work was reported.")}

## Behaviour

| Scenario | Before | After |
| --- | --- | --- |
| Ticket scenario | {root_cause} | {summary} |

## Confidence

**Overall confidence:** `{overall}` — `{confidence_pct}%`

| Area | Confidence | Reason |
| --- | ---: | --- |
| Root cause | `{confidence_pct}%` | Supported by the investigation evidence above. |
| Fix | `{confidence_pct}%` | The implementation passed the submission confidence gate. |
| Testing | `{'100' if all_passed else confidence_pct}%` | See the reported automated checks below. |
| Requirements | `{confidence_pct}%` | Assessed against issue #{issue_number}. |

### Remaining Unknowns

{bullets(remaining, "None reported.")}

## Risk

**Risk level:** `{risk_level}`

### Main Risks

{bullets(risks or [item.get("title", item.get("message", "Review finding")) for item in findings], "No material risks were reported.")}

### Mitigation

* Automated investigation, validation, and code review were completed before PR creation.

## Testing

### Automated Checks

* [{check}] Relevant reported checks pass
* [ ] Existing tests pass
* [ ] Lint passes
* [ ] Type checking passes
* [ ] Build passes

{test_report}

### Manual Verification

{manual}

### Edge Cases Checked

* [ ] Empty or missing data
* [ ] Invalid input
* [ ] Existing or duplicate records
* [ ] Permission restrictions
* [ ] Failure and rollback behaviour
* [ ] Related integrations

## Data and Security

* **Database changes:** `Not reported`
* **Migration required:** `Not reported`
* **Permissions changed:** `Not reported`
* **Sensitive data affected:** `Not reported`
* **Rollback safe:** `Not reported`

## Reviewer Focus

Please review:

{reviewer_list}

Additional notes: {notes}

## Deployment

* No deployment steps or configuration changes were reported.
* Monitor the changed behaviour after release.

## Rollback

`Revert the PR commit(s) and redeploy the previous revision.`

## Final Checklist

* [x] Issue and PR are linked
* [x] Root cause is documented
* [x] Change is limited to the reported scope
* [{check}] Tests and validation completed
* [x] Risks and unknowns are documented
* [ ] No unapproved schema changes
* [x] Rollback approach is clear
""".strip()
