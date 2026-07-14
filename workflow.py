from __future__ import annotations

import shutil
import shlex
import subprocess
import threading
import time
from pathlib import Path

from config import Settings
from core import (
    WorkflowError,
    blocking_findings,
    command_exists,
    ensure_keys,
    format_review_markdown,
    generate_test_plan,
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
from prompts import confidence_gate_prompt, investigation_prompt, repair_prompt, review_prompt
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

    def _provider_command(self, provider: str, custom: str | None, role: str) -> str:
        if provider == "claude":
            return self.settings.claude_command
        if provider == "codex":
            return self.settings.agent_command if role == "agent" else self.settings.review_command
        return (custom or "").strip()

    def stage(self, job_id: str, name: str) -> None:
        self.store.update(job_id, stage=name)
        self.log(job_id, f"\n== {name} ==")

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
            issue_ref = parse_issue_url(params["issue_url"])
            base_branch = validate_ref_name(params["base_branch"], "base branch")
            branch_prefix = params.get("branch_prefix", "bug-fix")
            agent_provider = params.get("agent_provider") or ("custom" if params.get("agent_command") else "codex")
            review_provider = params.get("review_provider") or ("custom" if params.get("review_command") else "codex")
            agent_command = self._provider_command(agent_provider, params.get("agent_command"), "agent")
            review_command = self._provider_command(review_provider, params.get("review_command"), "review")
            validation_commands = parse_validation_commands(params.get("validation_commands", ""))

            for required in ("git", "gh"):
                if shutil.which(required) is None:
                    raise WorkflowError(f"Required executable is not installed or on PATH: {required}")
            if not command_exists(agent_command):
                raise WorkflowError(f"Agent executable is not installed or on PATH: {agent_command}")
            if not command_exists(review_command):
                raise WorkflowError(f"Review executable is not installed or on PATH: {review_command}")

            artifact_dir = self.settings.workspace_root / job_id
            workspace_dir: Path
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

            self.approve_stage(job_id, "Checking GitHub access")
            self.stage(job_id, "Checking GitHub access")
            github.check_auth()

            self.approve_stage(job_id, "Reading ticket")
            self.stage(job_id, "Reading ticket")
            issue = github.get_issue(issue_ref)
            repository = github.get_repository(issue_ref)
            branch_name = make_branch_name(branch_prefix, issue_ref.number, issue["title"])
            test_plan = self._get_or_generate_test_plan(issue_ref, issue, agent_command, artifact_dir, log)

            repo_dirs: dict[str, Path] = {issue_ref.repo: repo_dir}
            if self.settings.local_repo_path and issue_ref.repo in {"crm-staff-desktop", "crm-api"}:
                for paired_name in ("crm-staff-desktop", "crm-api"):
                    paired_dir = workspace_dir / paired_name
                    if not (paired_dir / ".git").exists():
                        raise WorkflowError(f"Paired CRM repository was not found at {paired_dir}.")
                    repo_dirs[paired_name] = paired_dir

            if not self.settings.local_repo_path:
                self.approve_stage(job_id, "Cloning repository")
                self.stage(job_id, "Cloning repository")
                github.clone(issue_ref, repo_dir)
            for repo_name, current_repo_dir in repo_dirs.items():
                log(f"Preparing repository: {repo_name}")
                github.prepare_branch(current_repo_dir, base_branch, branch_name)
            # When working from a local repository, open the enclosing application
            # workspace so both the desktop and API repositories are visible in VS Code.
            # Git operations and the coding agent remain scoped to repo_dir.
            editor_dir = (
                self.settings.local_repo_path
                if self.settings.local_repo_path
                else repo_dir
            )
            self._open_editor(editor_dir, log)

            self.approve_stage(job_id, "Investigating and implementing")
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
            )
            (artifact_dir / "investigation-prompt.md").write_text(initial_prompt, encoding="utf-8")
            result = self._run_agent_gated(
                job_id, agent_command, workspace_dir, result_path, issue, initial_prompt, log
            )
            changed_repos = {
                name: path for name, path in repo_dirs.items() if github.has_changes(path)
            }
            if not changed_repos:
                raise WorkflowError("The coding agent completed without producing any source changes.")
            log("Repositories changed: " + ", ".join(changed_repos))

            self.approve_stage(job_id, "Running validation")
            self.stage(job_id, "Running validation")
            for repo_name, current_repo_dir in changed_repos.items():
                log(f"Validating repository: {repo_name}")
                github.validate_diff(current_repo_dir)
                for command in validation_commands:
                    run_command(
                        command,
                        cwd=current_repo_dir,
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

            reviews = {
                name: self._review(job_id, path, issue, base_branch, review_command, log)
                for name, path in changed_repos.items()
            }
            review = {"verdict": "PASS", "summary": "All changed repositories reviewed.", "findings": []}
            for repo_name, repo_review in reviews.items():
                for finding in repo_review.get("findings") or []:
                    review["findings"].append({**finding, "repository": repo_name})
            if review["findings"]:
                review["verdict"] = "BLOCK" if blocking_findings(review) else "COMMENT"
            cycles = 0
            while blocking_findings(review) and cycles < self.settings.max_repair_cycles:
                cycles += 1
                repair_stage = f"Repairing review findings ({cycles}/{self.settings.max_repair_cycles})"
                self.approve_stage(job_id, repair_stage)
                self.stage(job_id, repair_stage)
                result = self._run_agent_gated(
                    job_id, agent_command, workspace_dir, result_path, issue,
                    repair_prompt(issue, review, str(result_path)), log,
                )
                self.approve_stage(job_id, "Re-running validation")
                self.stage(job_id, "Re-running validation")
                changed_repos = {name: path for name, path in repo_dirs.items() if github.has_changes(path)}
                for current_repo_dir in changed_repos.values():
                    github.validate_diff(current_repo_dir)
                    for command in validation_commands:
                        run_command(command, cwd=current_repo_dir, timeout=self.settings.command_timeout_seconds, log=log)
                reviews = {
                    name: self._review(job_id, path, issue, base_branch, review_command, log)
                    for name, path in changed_repos.items()
                }
                review = {"verdict": "PASS", "summary": "All changed repositories reviewed.", "findings": []}
                for repo_name, repo_review in reviews.items():
                    for finding in repo_review.get("findings") or []:
                        review["findings"].append({**finding, "repository": repo_name})
                if review["findings"]:
                    review["verdict"] = "BLOCK" if blocking_findings(review) else "COMMENT"

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
                }
                self.store.update(job_id, status="completed", stage="Code written (PR skipped)", result_json=local_result)
                log("Completed: code written locally; PR creation skipped by strategy.")
                return

            blockers = blocking_findings(review)
            if blockers:
                titles = ", ".join(str(item.get("title", "blocking finding")) for item in blockers)
                raise WorkflowError(f"Automated review still has blocking findings: {titles}")

            self.approve_stage(job_id, "Committing and pushing")
            self.stage(job_id, "Committing and pushing")
            issue_link = f"{issue_ref.owner}/{issue_ref.repo}#{issue_ref.number}"
            repo_refs = {
                name: type(issue_ref)(owner=issue_ref.owner, repo=name, number=issue_ref.number)
                for name in changed_repos
            }
            repo_metadata = {
                name: github.get_repository(ref) for name, ref in repo_refs.items()
            }
            for repo_name, current_repo_dir in changed_repos.items():
                log(f"Committing repository: {repo_name}")
                github.commit_and_push(
                    current_repo_dir,
                    branch_name,
                    str(result["commit_message"]),
                    repo_refs[repo_name].full_name,
                )

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
                # ponytail: closing keyword is the only body-based way to link a PR
                # into the Development section; keep it for the issue's own repo.
                relation = "Fixes" if repo_name == issue_ref.repo else "Relates to"
                repo_review = reviews[repo_name]
                pr_body = self._build_pr_body(
                    result,
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
                "review": review,
            }
            self.store.update(job_id, result_json=partial_result)

            self.stage(job_id, "Posting code review")
            for repo_name, current_pr_url in pr_urls.items():
                repo_artifact_dir = artifact_dir / repo_name
                github.post_review(
                    repo_refs[repo_name],
                    current_pr_url,
                    reviews[repo_name],
                    format_review_markdown(reviews[repo_name]),
                    repo_artifact_dir,
                )

            self.stage(job_id, "Linking original ticket")
            github.comment_on_issue(
                issue_ref,
                "Automated investigation and coordinated fix PRs created:\n\n"
                + "\n".join(f"- `{name}`: {url}" for name, url in pr_urls.items())
                + f"\n\nBase branch: `{base_branch}`  \nBranch: `{branch_name}`"
                + self._format_test_plan_markdown(test_plan),
                artifact_dir,
            )

            github_login = params.get("github_login")
            if github_login:
                github.assign_issue(issue_ref, github_login)
                for repo_name, current_pr_url in pr_urls.items():
                    github.assign_pr(repo_refs[repo_name], current_pr_url, github_login)

            additions = deletions = 0
            for current_repo_dir in changed_repos.values():
                repo_additions, repo_deletions = github.diff_stat(current_repo_dir, base_branch)
                additions += repo_additions
                deletions += repo_deletions

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
                "review": review,
                "assignee": github_login,
                "additions": additions,
                "deletions": deletions,
                "duration_seconds": round(time.time() - started_at),
            }
            self.store.update(job_id, status="completed", stage="Completed", result_json=final_result)
            log("Completed: " + ", ".join(pr_urls.values()))
        except Exception as exc:  # noqa: BLE001 - workflow boundary must record every failure
            current = self.store.get(job_id)
            if not current or current.get("status") != "stopped":
                self.store.update(job_id, status="failed", stage="Failed", error=str(exc))
                if params.get("comment_on_failure", self.settings.comment_on_failure):
                    self._comment_on_failure(job_id, params, exc, current, result, review, log)
            log(f"ERROR: {exc}")

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
    def _failure_comment(failed_stage: str, error: str, result: dict, review: dict) -> str:
        category, explanation = WorkflowRunner._classify_failure(error, result, review)

        requirements = [str(item) for item in (result.get("completion_requirements") or []) if str(item).strip()]
        if not requirements:
            requirements.extend(str(item) for item in (result.get("unresolved_risks") or []) if str(item).strip())
        for test in result.get("tests_run") or []:
            if str(test.get("result", "")).lower() == "failed":
                requirements.append(
                    f"Fix `{test.get('command', 'the failing validation')}`: {test.get('notes') or 'the check did not pass.'}"
                )
        for finding in review.get("findings") or []:
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

        tests = result.get("tests_run") or []
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

    def _get_or_generate_test_plan(
        self, issue_ref, issue: dict, agent_command: str, artifact_dir: Path, log,
    ) -> dict | None:
        key = f"{issue_ref.full_name}#{issue_ref.number}"
        cached = self.store.get_ticket_test(key)
        if cached:
            return cached
        try:
            plan = generate_test_plan(
                agent_command, self.settings.workspace_root, issue,
                artifact_dir / "test-plan.json", self.settings.review_timeout_seconds, log,
            )
        except WorkflowError as exc:
            log(f"Could not generate a test plan for the ticket: {exc}")
            return None
        self.store.upsert_ticket_test(key, plan["repro_steps"], plan["pass_steps"])
        return plan

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

    def _review(self, job_id: str, repo_dir: Path, issue: dict, base_branch: str, command: str, log) -> dict:
        self.approve_stage(job_id, "Reviewing the change")
        self.stage(job_id, "Reviewing the change")
        review_path = repo_dir / ".ticket-agent" / "review.json"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.unlink(missing_ok=True)
        before = working_tree_fingerprint(repo_dir)
        prompt = review_prompt(issue, base_branch)
        # The coding agent may clean its artifact directory as part of its
        # teardown. Recreate it immediately before writing the review prompt so
        # review cannot fail with a stale/missing .ticket-agent path.
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.with_name("review-prompt.md").write_text(prompt, encoding="utf-8")
        run_configured_command(
            command,
            cwd=repo_dir,
            prompt=prompt,
            timeout=self.settings.review_timeout_seconds,
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

    def _run_agent_gated(
        self, job_id: str, agent_command: str, workspace_dir: Path,
        result_path: Path, issue: dict, prompt: str, log,
    ) -> dict:
        """Run the coding agent, and on a gate failure (low confidence, unsafe,
        unresolved risks, failed checks) loop with a repair prompt until the
        result passes the confidence gate, up to max_gate_attempts."""
        attempt = 0
        while True:
            attempt += 1
            result_path.unlink(missing_ok=True)
            run_configured_command(
                agent_command,
                cwd=workspace_dir,
                prompt=prompt,
                timeout=self.settings.command_timeout_seconds,
                log=log,
            )
            try:
                result = load_json(result_path)
                ensure_keys(
                    result,
                    ["safe_to_pr", "confidence", "summary", "root_cause", "tests_run", "unresolved_risks", "commit_message", "pr_title"],
                    "Agent result",
                )
                self._log_agent_result(result, log)
                self._gate_agent_result(result)
                return result
            except WorkflowError as exc:
                job = self.store.get(job_id)
                if not job or job["status"] == "stopped":
                    raise
                if attempt >= self.settings.max_gate_attempts:
                    raise WorkflowError(
                        f"Gate still failing after {attempt} attempts: {exc}"
                    ) from exc
                log(
                    f"Gate failed on attempt {attempt}/{self.settings.max_gate_attempts}: {exc} "
                    f"Retrying until confidence >= {self.settings.minimum_confidence:.2f} and the change is safe to PR."
                )
                prompt = confidence_gate_prompt(issue, str(exc), str(result_path))

    def _gate_agent_result(self, result: dict) -> None:
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
            item for item in (result.get("tests_run") or []) if str(item.get("result", "")).lower() == "failed"
        ]
        if failed_tests:
            raise WorkflowError("The coding agent reported failed validation checks.")

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
