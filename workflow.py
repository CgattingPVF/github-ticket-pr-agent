from __future__ import annotations

import shutil
import shlex
import subprocess
import threading
from pathlib import Path

from config import Settings
from core import (
    WorkflowError,
    blocking_findings,
    command_exists,
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
from github_ops import GitHubOps
from prompts import investigation_prompt, repair_prompt, review_prompt
from store import JobStore


class WorkflowRunner:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store

    def start(self, job_id: str) -> None:
        thread = threading.Thread(target=self.run, args=(job_id,), daemon=True)
        thread.start()

    def log(self, job_id: str, message: str) -> None:
        self.store.append_log(job_id, message)

    def stage(self, job_id: str, name: str) -> None:
        self.store.update(job_id, stage=name)
        self.log(job_id, f"\n== {name} ==")

    def run(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        params = job["parameters"]
        def log(message: str) -> None:
            self.log(job_id, message)
        self.store.update(job_id, status="running", stage="Starting")

        try:
            issue_ref = parse_issue_url(params["issue_url"])
            base_branch = validate_ref_name(params["base_branch"], "base branch")
            branch_prefix = params.get("branch_prefix", "bug-fix")
            agent_command = params.get("agent_command") or self.settings.agent_command
            review_command = params.get("review_command") or self.settings.review_command
            validation_commands = parse_validation_commands(params.get("validation_commands", ""))
            close_issue_on_merge = bool(params.get("close_issue_on_merge", False))

            for required in ("git", "gh"):
                if shutil.which(required) is None:
                    raise WorkflowError(f"Required executable is not installed or on PATH: {required}")
            if not command_exists(agent_command):
                raise WorkflowError(f"Agent executable is not installed or on PATH: {agent_command}")
            if not command_exists(review_command):
                raise WorkflowError(f"Review executable is not installed or on PATH: {review_command}")

            artifact_dir = self.settings.workspace_root / job_id
            repo_dir = artifact_dir / "repo"
            artifact_dir.mkdir(parents=True, exist_ok=False)
            github = GitHubOps(self.settings.command_timeout_seconds, log)

            self.stage(job_id, "Checking GitHub access")
            github.check_auth()

            self.stage(job_id, "Reading ticket")
            issue = github.get_issue(issue_ref)
            repository = github.get_repository(issue_ref)
            branch_name = make_branch_name(branch_prefix, issue_ref.number, issue["title"])

            self.stage(job_id, "Cloning repository")
            github.clone(issue_ref, repo_dir)
            github.prepare_branch(repo_dir, base_branch, branch_name)
            self._open_editor(repo_dir, log)

            self.stage(job_id, "Investigating and implementing")
            initial_prompt = investigation_prompt(issue, base_branch, branch_name)
            (artifact_dir / "investigation-prompt.md").write_text(initial_prompt, encoding="utf-8")
            run_configured_command(
                agent_command,
                cwd=repo_dir,
                prompt=initial_prompt,
                timeout=self.settings.command_timeout_seconds,
                log=log,
            )
            result_path = repo_dir / ".ticket-agent" / "result.json"
            result = load_json(result_path)
            ensure_keys(
                result,
                ["safe_to_pr", "confidence", "summary", "root_cause", "tests_run", "unresolved_risks", "commit_message", "pr_title"],
                "Agent result",
            )

            self._gate_agent_result(result)
            if not github.has_changes(repo_dir):
                raise WorkflowError("The coding agent completed without producing any source changes.")

            self.stage(job_id, "Running validation")
            github.validate_diff(repo_dir)
            for command in validation_commands:
                run_command(
                    command,
                    cwd=repo_dir,
                    timeout=self.settings.command_timeout_seconds,
                    log=log,
                )
            if not validation_commands:
                reported_passes = [
                    item for item in (result.get("tests_run") or [])
                    if str(item.get("result", "")).lower() == "passed"
                ]
                if not reported_passes:
                    raise WorkflowError(
                        "No validation command was configured and the agent reported no passing check."
                    )
                log("No extra validation commands were configured; relying on `git diff --check` and agent-reported passing checks.")

            review = self._review(job_id, repo_dir, issue, base_branch, review_command, log)
            cycles = 0
            while blocking_findings(review) and cycles < self.settings.max_repair_cycles:
                cycles += 1
                self.stage(job_id, f"Repairing review findings ({cycles}/{self.settings.max_repair_cycles})")
                run_configured_command(
                    agent_command,
                    cwd=repo_dir,
                    prompt=repair_prompt(issue, review),
                    timeout=self.settings.command_timeout_seconds,
                    log=log,
                )
                result = load_json(result_path)
                ensure_keys(
                    result,
                    ["safe_to_pr", "confidence", "summary", "root_cause", "tests_run", "unresolved_risks", "commit_message", "pr_title"],
                    "Agent result",
                )
                self._gate_agent_result(result)
                self.stage(job_id, "Re-running validation")
                github.validate_diff(repo_dir)
                for command in validation_commands:
                    run_command(command, cwd=repo_dir, timeout=self.settings.command_timeout_seconds, log=log)
                review = self._review(job_id, repo_dir, issue, base_branch, review_command, log)

            blockers = blocking_findings(review)
            if blockers:
                titles = ", ".join(str(item.get("title", "blocking finding")) for item in blockers)
                raise WorkflowError(f"Automated review still has blocking findings: {titles}")

            self.stage(job_id, "Committing and pushing")
            github.commit_and_push(
                repo_dir, branch_name, str(result["commit_message"]), issue_ref.full_name
            )

            issue_link = f"{issue_ref.owner}/{issue_ref.repo}#{issue_ref.number}"
            relation = "Fixes" if close_issue_on_merge and base_branch == repository.get("default_branch") else "Relates to"
            review_md = format_review_markdown(review)
            pr_body = self._build_pr_body(result, review_md, relation, issue_link, base_branch, repository.get("default_branch"))

            self.stage(job_id, "Creating pull request")
            pr_url = github.create_pr(
                issue_ref,
                repo_dir,
                base_branch,
                branch_name,
                str(result["pr_title"]),
                pr_body,
                artifact_dir,
            )

            partial_result = {
                "ticket_url": issue["html_url"],
                "repository": issue_ref.full_name,
                "issue_number": issue_ref.number,
                "base_branch": base_branch,
                "branch_name": branch_name,
                "pr_url": pr_url,
                "confidence": result["confidence"],
                "summary": result["summary"],
                "root_cause": result["root_cause"],
                "review": review,
            }
            self.store.update(job_id, result_json=partial_result)

            self.stage(job_id, "Posting code review")
            github.post_review(issue_ref, pr_url, review, review_md, artifact_dir)

            self.stage(job_id, "Linking original ticket")
            github.comment_on_issue(
                issue_ref,
                f"Automated investigation and fix PR created: {pr_url}\n\nBase branch: `{base_branch}`  \nBranch: `{branch_name}`",
                artifact_dir,
            )

            final_result = {
                "ticket_url": issue["html_url"],
                "repository": issue_ref.full_name,
                "issue_number": issue_ref.number,
                "base_branch": base_branch,
                "branch_name": branch_name,
                "pr_url": pr_url,
                "confidence": result["confidence"],
                "summary": result["summary"],
                "root_cause": result["root_cause"],
                "review": review,
            }
            self.store.update(job_id, status="completed", stage="Completed", result_json=final_result)
            log(f"Completed: {pr_url}")
        except Exception as exc:  # noqa: BLE001 - workflow boundary must record every failure
            self.store.update(job_id, status="failed", stage="Failed", error=str(exc))
            log(f"ERROR: {exc}")

    def _review(self, job_id: str, repo_dir: Path, issue: dict, base_branch: str, command: str, log) -> dict:
        self.stage(job_id, "Reviewing the change")
        review_path = repo_dir / ".ticket-agent" / "review.json"
        review_path.unlink(missing_ok=True)
        before = working_tree_fingerprint(repo_dir)
        prompt = review_prompt(issue, base_branch)
        (repo_dir / ".ticket-agent" / "review-prompt.md").write_text(prompt, encoding="utf-8")
        run_configured_command(
            command,
            cwd=repo_dir,
            prompt=prompt,
            timeout=self.settings.command_timeout_seconds,
            log=log,
        )
        after = working_tree_fingerprint(repo_dir)
        if before != after:
            raise WorkflowError("The review agent modified source files; review must be read-only.")
        review = load_json(review_path)
        ensure_keys(review, ["verdict", "summary", "findings"], "Review result")
        if not isinstance(review.get("findings"), list):
            raise WorkflowError("Review findings must be a JSON array.")
        return review

    def _open_editor(self, repo_dir: Path, log) -> None:
        command = shlex.split(self.settings.editor_command)
        if not command or shutil.which(command[0]) is None:
            log("VS Code opener is unavailable; continuing with the automated workflow.")
            return
        try:
            subprocess.Popen([*command, str(repo_dir)], cwd=repo_dir)
            log(f"Opened workspace in editor: {repo_dir}")
        except OSError as exc:
            log(f"Could not open workspace in editor: {exc}")

    def _gate_agent_result(self, result: dict) -> None:
        try:
            confidence = float(result["confidence"])
        except (TypeError, ValueError) as exc:
            raise WorkflowError("Agent confidence must be a number between 0 and 1.") from exc
        if not 0 <= confidence <= 1:
            raise WorkflowError("Agent confidence must be between 0 and 1.")
        if result["safe_to_pr"] is not True:
            raise WorkflowError("The coding agent marked the change as unsafe to submit as a PR.")
        if confidence < self.settings.minimum_confidence:
            raise WorkflowError(
                f"Agent confidence {confidence:.2f} is below the required {self.settings.minimum_confidence:.2f}."
            )
        risks = result.get("unresolved_risks") or []
        if risks:
            raise WorkflowError("Unresolved risks remain: " + "; ".join(map(str, risks)))
        failed_tests = [
            item for item in (result.get("tests_run") or []) if str(item.get("result", "")).lower() == "failed"
        ]
        if failed_tests:
            raise WorkflowError("The coding agent reported failed validation checks.")

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
        for item in tests:
            test_lines.append(
                f"- `{item.get('command', 'not specified')}`: **{item.get('result', 'unknown')}**"
                + (f" — {item.get('notes')}" if item.get("notes") else "")
            )
        if not test_lines:
            test_lines = ["- No test commands were reported."]
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
