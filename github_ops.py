from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from core import IssueRef, WorkflowError, dump_json, run_command


class GitHubOps:
    def __init__(self, timeout: int, log: Callable[[str], None]):
        self.timeout = timeout
        self.log = log

    def check_auth(self) -> None:
        run_command(["gh", "auth", "status"], timeout=self.timeout, log=self.log)

    def get_issue(self, ref: IssueRef) -> dict:
        result = run_command(
            ["gh", "api", f"repos/{ref.full_name}/issues/{ref.number}"],
            timeout=self.timeout,
            log=self.log,
        )
        issue = json.loads(result.stdout)
        if "pull_request" in issue:
            raise WorkflowError("The supplied URL is a pull request, not an issue ticket.")
        if issue.get("state") != "open":
            raise WorkflowError("The supplied GitHub ticket is not open.")
        return issue

    def get_repository(self, ref: IssueRef) -> dict:
        result = run_command(
            ["gh", "api", f"repos/{ref.full_name}"],
            timeout=self.timeout,
            log=self.log,
        )
        return json.loads(result.stdout)

    def clone(self, ref: IssueRef, destination: Path) -> None:
        run_command(
            ["gh", "repo", "clone", ref.full_name, str(destination), "--", "--filter=blob:none"],
            timeout=self.timeout,
            log=self.log,
        )

    def prepare_branch(self, repo_dir: Path, base_branch: str, branch_name: str) -> None:
        existing = run_command(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", branch_name],
            cwd=repo_dir, timeout=self.timeout, log=self.log, check=False
        )
        if existing.returncode == 0:
            raise WorkflowError(f"Remote branch already exists: {branch_name}")
        run_command(["git", "fetch", "origin", base_branch], cwd=repo_dir, timeout=self.timeout, log=self.log)
        run_command(["git", "checkout", "-B", branch_name, "FETCH_HEAD"], cwd=repo_dir, timeout=self.timeout, log=self.log)
        exclude = repo_dir / ".git" / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\n.ticket-agent/\n")

    def ensure_commit_identity(self, repo_dir: Path) -> None:
        email = run_command(
            ["git", "config", "user.email"], cwd=repo_dir, timeout=self.timeout, log=self.log, check=False
        ).stdout.strip()
        name = run_command(
            ["git", "config", "user.name"], cwd=repo_dir, timeout=self.timeout, log=self.log, check=False
        ).stdout.strip()
        if not email:
            run_command(
                ["git", "config", "user.email", "ticket-agent@localhost"],
                cwd=repo_dir,
                timeout=self.timeout,
                log=self.log,
            )
        if not name:
            run_command(
                ["git", "config", "user.name", "Ticket PR Agent"],
                cwd=repo_dir,
                timeout=self.timeout,
                log=self.log,
            )

    def has_changes(self, repo_dir: Path) -> bool:
        result = run_command(
            ["git", "status", "--porcelain"], cwd=repo_dir, timeout=self.timeout, log=self.log
        )
        return bool(result.stdout.strip())

    def diff(self, repo_dir: Path, base_branch: str) -> str:
        return run_command(
            ["git", "diff", f"origin/{base_branch}...HEAD"],
            cwd=repo_dir,
            timeout=self.timeout,
            log=self.log,
        ).stdout

    def validate_diff(self, repo_dir: Path) -> None:
        run_command(["git", "diff", "--check"], cwd=repo_dir, timeout=self.timeout, log=self.log)

    def commit_and_push(
        self, repo_dir: Path, branch_name: str, commit_message: str, expected_repository: str
    ) -> None:
        origin = run_command(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_dir, timeout=self.timeout, log=self.log
        ).stdout.strip()
        accepted = {
            f"https://github.com/{expected_repository}.git",
            f"https://github.com/{expected_repository}",
            f"git@github.com:{expected_repository}.git",
        }
        if origin not in accepted:
            raise WorkflowError(f"Origin remote changed unexpectedly: {origin}")
        safe_hooks = repo_dir.parent / "empty-git-hooks"
        safe_hooks.mkdir(exist_ok=True)
        run_command(
            ["git", "config", "core.hooksPath", str(safe_hooks)],
            cwd=repo_dir, timeout=self.timeout, log=self.log
        )
        self.ensure_commit_identity(repo_dir)
        run_command(["git", "add", "-A"], cwd=repo_dir, timeout=self.timeout, log=self.log)
        run_command(["git", "commit", "-m", commit_message], cwd=repo_dir, timeout=self.timeout, log=self.log)
        run_command(
            ["git", "push", "--set-upstream", "origin", branch_name],
            cwd=repo_dir,
            timeout=self.timeout,
            log=self.log,
        )

    def create_pr(
        self,
        ref: IssueRef,
        repo_dir: Path,
        base_branch: str,
        branch_name: str,
        title: str,
        body: str,
        artifact_dir: Path,
    ) -> str:
        body_file = artifact_dir / "pr-body.md"
        body_file.write_text(body, encoding="utf-8")
        result = run_command(
            [
                "gh", "pr", "create", "--repo", ref.full_name,
                "--base", base_branch, "--head", branch_name,
                "--title", title, "--body-file", str(body_file),
            ],
            cwd=repo_dir,
            timeout=self.timeout,
            log=self.log,
        )
        url = result.stdout.strip().splitlines()[-1]
        if not url.startswith("https://github.com/"):
            raise WorkflowError("GitHub CLI did not return a pull request URL.")
        return url

    def comment_on_issue(self, ref: IssueRef, body: str, artifact_dir: Path) -> None:
        body_file = artifact_dir / "issue-comment.md"
        body_file.write_text(body, encoding="utf-8")
        run_command(
            ["gh", "issue", "comment", str(ref.number), "--repo", ref.full_name, "--body-file", str(body_file)],
            timeout=self.timeout,
            log=self.log,
        )

    def post_review(self, ref: IssueRef, pr_url: str, review: dict, body: str, artifact_dir: Path) -> None:
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        pr = json.loads(
            run_command(
                ["gh", "api", f"repos/{ref.full_name}/pulls/{pr_number}"],
                timeout=self.timeout,
                log=self.log,
            ).stdout
        )
        comments = []
        for finding in review.get("findings") or []:
            path = finding.get("path")
            line = finding.get("line")
            if path and isinstance(line, int) and line > 0:
                comments.append(
                    {
                        "path": path,
                        "line": line,
                        "side": "RIGHT",
                        "body": f"**{str(finding.get('severity', 'INFO')).upper()}: {finding.get('title', 'Review finding')}**\n\n{finding.get('body', '')}",
                    }
                )
        payload = {
            "commit_id": pr["head"]["sha"],
            "body": body,
            "event": "COMMENT",
            "comments": comments,
        }
        payload_path = artifact_dir / "review-payload.json"
        dump_json(payload_path, payload)
        result = run_command(
            [
                "gh", "api", "--method", "POST",
                f"repos/{ref.full_name}/pulls/{pr_number}/reviews",
                "--input", str(payload_path),
            ],
            timeout=self.timeout,
            log=self.log,
            check=False,
        )
        if result.returncode != 0:
            self.log("Inline review submission failed; posting the review as an overall PR review instead.")
            body_file = artifact_dir / "review.md"
            body_file.write_text(body, encoding="utf-8")
            run_command(
                [
                    "gh", "pr", "review", str(pr_number), "--repo", ref.full_name,
                    "--comment", "--body-file", str(body_file),
                ],
                timeout=self.timeout,
                log=self.log,
            )
