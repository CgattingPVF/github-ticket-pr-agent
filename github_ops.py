from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

from core import IssueRef, WorkflowError, dump_json, run_command


TEST_DIRECTORIES = {"test", "tests", "spec", "specs", "__tests__"}
SCHEMA_DIRECTORIES = {"migration", "migrations", "schema", "schemas"}


def is_test_file(path: str) -> bool:
    """Return whether a repository-relative path conventionally contains tests."""
    parts = Path(path).parts
    if any(part.lower() in TEST_DIRECTORIES for part in parts[:-1]):
        return True
    filename = parts[-1] if parts else path
    stem = Path(filename).stem
    return bool(
        re.search(r"(^test[_.-]|[_.-](?:test|tests|spec|specs)(?:\.|$))", filename, re.IGNORECASE)
        or re.search(r"(?:Test|Tests|Spec|Specs)$", stem)
    )


def is_schema_file(path: str) -> bool:
    """Return whether a changed path represents SQL or an explicit schema change."""
    parts = Path(path).parts
    filename = parts[-1].lower() if parts else path.lower()
    return (
        filename.endswith('.sql')
        or filename.startswith('schema.')
        or any(part.lower() in SCHEMA_DIRECTORIES for part in parts[:-1])
    )


class GitHubOps:
    def __init__(self, timeout: int, log: Callable[[str], None]):
        self.timeout = timeout
        self.log = log

    def check_auth(self) -> None:
        run_command(["gh", "auth", "status"], timeout=self.timeout, log=self.log)

    def get_issue(self, ref: IssueRef, *, require_open: bool = True) -> dict:
        result = run_command(
            ["gh", "api", f"repos/{ref.full_name}/issues/{ref.number}"],
            timeout=self.timeout,
            log=self.log,
        )
        issue = json.loads(result.stdout)
        if "pull_request" in issue:
            raise WorkflowError("The supplied URL is a pull request, not an issue ticket.")
        if require_open and issue.get("state") != "open":
            raise WorkflowError("The supplied GitHub ticket is not open.")
        return issue

    def get_issue_with_compact_context(self, ref: IssueRef) -> dict:
        """Read the ticket and its bounded delivery context in one GitHub call.

        Full Delivery used to launch three ``gh`` processes here: the REST issue,
        comments, and a linked-PR GraphQL query. A single bounded GraphQL query is
        both faster and materially smaller than carrying those responses through
        separate agent prompts.
        """
        query = f'''query {{
          repository(owner: "{ref.owner}", name: "{ref.repo}") {{
            defaultBranchRef {{ name }}
            issue(number: {ref.number}) {{
              number title body state url updatedAt
              labels(first: 20) {{ nodes {{ name }} }}
              comments(last: 5) {{ nodes {{
                author {{ login }} createdAt body
              }} }}
              closedByPullRequestsReferences(first: 5) {{ nodes {{
                number title url state changedFiles
                baseRefName headRefName
                repository {{ nameWithOwner }}
                commits(last: 1) {{ nodes {{
                  commit {{
                    statusCheckRollup {{
                      contexts(first: 8) {{ nodes {{
                        ... on CheckRun {{ name conclusion status }}
                        ... on StatusContext {{ context state }}
                      }} }}
                    }}
                  }}
                }} }}
              }} }}
            }}
          }}
        }}'''
        result = run_command(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            timeout=self.timeout,
            log=self.log,
        )
        try:
            repository = json.loads(result.stdout)["data"]["repository"]
            node = repository["issue"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise WorkflowError("GitHub did not return the requested issue ticket.") from exc
        if not node:
            raise WorkflowError("The supplied GitHub ticket was not found.")
        if str(node.get("state", "")).lower() != "open":
            raise WorkflowError("The supplied GitHub ticket is not open.")

        context = {
            "latest_comments": [
                {
                    "author": (comment.get("author") or {}).get("login"),
                    "created_at": comment.get("createdAt"),
                    "body": comment.get("body") or "",
                }
                for comment in ((node.get("comments") or {}).get("nodes") or [])
                if isinstance(comment, dict)
            ],
            "linked_pull_requests": self._format_linked_pull_request_nodes(
                (node.get("closedByPullRequestsReferences") or {}).get("nodes") or []
            ),
            "context_warnings": [],
        }
        return {
            "html_url": node.get("url"),
            "number": node.get("number"),
            "title": node.get("title"),
            "body": node.get("body") or "",
            "state": str(node.get("state", "")).lower(),
            "updated_at": node.get("updatedAt"),
            "labels": [
                {"name": label.get("name")}
                for label in ((node.get("labels") or {}).get("nodes") or [])
                if isinstance(label, dict)
            ],
            "repository_default_branch": (repository.get("defaultBranchRef") or {}).get("name"),
            "mergequest_github_context": context,
        }

    def get_compact_issue_context(self, ref: IssueRef) -> dict:
        """Fetch bounded GitHub context directly through gh, without MCP."""
        context: dict[str, object] = {
            "latest_comments": [],
            "linked_pull_requests": [],
            "context_warnings": [],
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                "latest_comments": executor.submit(self._latest_issue_comments, ref),
                "linked_pull_requests": executor.submit(self._linked_pull_request_context, ref),
            }
            for key, future in futures.items():
                try:
                    context[key] = future.result()
                except Exception as exc:
                    label = "issue comments" if key == "latest_comments" else "linked pull requests"
                    context["context_warnings"].append(f"Could not read {label}: {exc}")
        return context

    def _latest_issue_comments(self, ref: IssueRef) -> list[dict]:
        result = run_command(
            ["gh", "api", f"repos/{ref.full_name}/issues/{ref.number}/comments?per_page=100"],
            timeout=self.timeout,
            log=self.log,
        )
        comments = json.loads(result.stdout)
        if not isinstance(comments, list):
            return []
        latest = comments[-5:]
        return [
            {
                "author": (comment.get("user") or {}).get("login"),
                "created_at": comment.get("created_at"),
                "body": comment.get("body") or "",
            }
            for comment in latest
            if isinstance(comment, dict)
        ]

    def _linked_pull_request_context(self, ref: IssueRef) -> list[dict]:
        query = f'''query {{
          repository(owner: "{ref.owner}", name: "{ref.repo}") {{
            issue(number: {ref.number}) {{
              closedByPullRequestsReferences(first: 5) {{ nodes {{
                number title url state changedFiles
                baseRefName headRefName
                repository {{ nameWithOwner }}
                commits(last: 1) {{ nodes {{
                  commit {{
                    statusCheckRollup {{
                      contexts(first: 8) {{ nodes {{
                        ... on CheckRun {{ name conclusion status }}
                        ... on StatusContext {{ context state }}
                      }} }}
                    }}
                  }}
                }} }}
              }} }}
            }}
          }}
        }}'''
        result = run_command(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            timeout=self.timeout,
            log=self.log,
        )
        nodes = (
            json.loads(result.stdout)
            .get("data", {})
            .get("repository", {})
            .get("issue", {})
            .get("closedByPullRequestsReferences", {})
            .get("nodes", [])
        )
        return self._format_linked_pull_request_nodes(nodes)

    @staticmethod
    def _format_linked_pull_request_nodes(nodes: list[dict]) -> list[dict]:
        items: list[dict] = []
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            check_nodes = (
                ((node.get("commits") or {}).get("nodes") or [{}])[-1]
                .get("commit", {})
                .get("statusCheckRollup", {})
                .get("contexts", {})
                .get("nodes", [])
            )
            checks = []
            for check in check_nodes or []:
                if not isinstance(check, dict):
                    continue
                checks.append({
                    "name": check.get("name") or check.get("context"),
                    "status": check.get("conclusion") or check.get("state") or check.get("status"),
                })
            items.append({
                "repository": (node.get("repository") or {}).get("nameWithOwner"),
                "number": node.get("number"),
                "title": node.get("title"),
                "state": node.get("state"),
                "url": node.get("url"),
                "base": node.get("baseRefName"),
                "head": node.get("headRefName"),
                "changed_files": node.get("changedFiles"),
                "checks": checks,
            })
        return items

    def get_repository(self, ref: IssueRef) -> dict:
        result = run_command(
            ["gh", "api", f"repos/{ref.full_name}"],
            timeout=self.timeout,
            log=self.log,
        )
        return json.loads(result.stdout)

    def set_issue_project_fields(self, ref: IssueRef, selections: dict[str, str | None]) -> dict[str, int]:
        """Set or clear named GitHub Projects v2 single-select fields on an issue."""
        query = f'''query {{
          repository(owner: "{ref.owner}", name: "{ref.repo}") {{
            issue(number: {ref.number}) {{
              projectItems(first: 20) {{ nodes {{
                id
                project {{
                  id
                  fields(first: 50) {{ nodes {{
                    ... on ProjectV2SingleSelectField {{ id name options {{ id name }} }}
                  }} }}
                }}
              }} }}
            }}
          }}
        }}'''
        result = run_command(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            timeout=self.timeout, log=self.log,
        )
        try:
            nodes = json.loads(result.stdout)["data"]["repository"]["issue"]["projectItems"]["nodes"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise WorkflowError("GitHub did not return project metadata for the ticket.") from exc

        wanted = {
            name.strip().lower(): value.strip() if value is not None else None
            for name, value in selections.items()
        }
        counts = {name: 0 for name in selections}
        updates: list[tuple[str, str, str, str | None, str]] = []
        for item in nodes or []:
            project = item.get("project") or {}
            for field in (project.get("fields") or {}).get("nodes", []):
                field_name = str(field.get("name", "")).strip()
                lookup = field_name.lower()
                # Some boards call the workflow field "Project Status".
                requested_key = "status" if lookup == "project status" and "status" in wanted else lookup
                if requested_key not in wanted:
                    continue
                display_key = next(key for key in selections if key.strip().lower() == requested_key)
                requested_value = wanted[requested_key]
                if requested_value is None:
                    updates.append((project["id"], item["id"], field["id"], None, display_key))
                    continue
                desired = requested_value.lower()
                accepted = {desired}
                if desired == "pass":
                    accepted.add("passed")
                elif desired == "done":
                    accepted.update({"complete", "completed"})
                option = next(
                    (choice for choice in field.get("options", []) if str(choice.get("name", "")).strip().lower() in accepted),
                    None,
                )
                if option:
                    updates.append((project["id"], item["id"], field["id"], option["id"], display_key))

        mutations: list[str] = []
        for index, (project_id, item_id, field_id, option_id, display_key) in enumerate(updates):
            if option_id is None:
                mutation = f'''field{index}: clearProjectV2ItemFieldValue(input: {{
                    projectId: "{project_id}", itemId: "{item_id}", fieldId: "{field_id}"
                  }}) {{ projectV2Item {{ id }} }}'''
            else:
                mutation = f'''field{index}: updateProjectV2ItemFieldValue(input: {{
                    projectId: "{project_id}", itemId: "{item_id}", fieldId: "{field_id}",
                    value: {{ singleSelectOptionId: "{option_id}" }}
                  }}) {{ projectV2Item {{ id }} }}'''
            mutations.append(mutation)
            counts[display_key] += 1
        if mutations:
            mutation = "mutation {\n" + "\n".join(mutations) + "\n}"
            run_command(
                ["gh", "api", "graphql", "-f", f"query={mutation}"],
                timeout=self.timeout, log=self.log,
            )
        return counts

    def mark_issue_qa_done(self, ref: IssueRef) -> dict[str, int]:
        """Mark a successful QA ticket Done with a passing Test State."""
        return self.set_issue_project_fields(ref, {"Status": "Done", "Test State": "Pass"})

    def mark_issue_pr_ready(self, ref: IssueRef) -> dict[str, int]:
        """Move a ticket with an open PR to PR Ready and clear its Test State."""
        return self.set_issue_project_fields(ref, {"Status": "PR Ready", "Test State": None})

    def has_linked_open_pr(self, ref: IssueRef, repositories: Iterable[str] | None = None) -> bool:
        """Return whether any named repository has an open PR linked to the ticket."""
        repository_names = list(dict.fromkeys(repositories or [ref.repo]))
        with ThreadPoolExecutor(max_workers=min(3, len(repository_names))) as executor:
            futures = [
                executor.submit(
                    self._linked_open_prs,
                    IssueRef(owner=ref.owner, repo=repository, number=ref.number),
                    ref,
                )
                for repository in repository_names
            ]
            return any(future.result() for future in futures)

    def sync_successful_qa_project_fields(
        self, ref: IssueRef, repositories: Iterable[str] | None = None,
    ) -> dict[str, object]:
        """Apply the successful-QA project transition for the ticket's live PR state."""
        has_open_pr = self.has_linked_open_pr(ref, repositories)
        status = "PR Ready" if has_open_pr else "Done"
        test_state = None if has_open_pr else "Pass"
        counts = self.mark_issue_pr_ready(ref) if has_open_pr else self.mark_issue_qa_done(ref)
        status_count = counts.get("Status", 0)
        test_state_count = counts.get("Test State", 0)
        return {
            "updated": bool(status_count and test_state_count),
            "count": status_count,
            "test_state_count": test_state_count,
            "status": status,
            "test_state": test_state,
            "has_open_pr": has_open_pr,
        }

    def set_issue_project_status(self, ref: IssueRef, status_name: str = "Done") -> int:
        """Backward-compatible helper for updating only the project Status."""
        return self.set_issue_project_fields(ref, {"Status": status_name})["Status"]

    def clone(self, ref: IssueRef, destination: Path) -> None:
        run_command(
            ["gh", "repo", "clone", ref.full_name, str(destination), "--", "--filter=blob:none"],
            timeout=self.timeout,
            log=self.log,
        )

    def prepare_branch(
        self, repo_dir: Path, base_branch: str, branch_name: str, *, clean_checked: bool = False,
    ) -> None:
        if not clean_checked and self.has_changes(repo_dir):
            raise WorkflowError(
                f"Repository {repo_dir.name} has uncommitted changes from another operation. "
                "Finish, stash, or discard that work before starting a new Contract."
            )
        # Remote uniqueness and base refresh are independent network operations.
        with ThreadPoolExecutor(max_workers=2) as executor:
            existing_future = executor.submit(
                run_command,
                ["git", "ls-remote", "--exit-code", "--heads", "origin", branch_name],
                cwd=repo_dir, timeout=self.timeout, log=self.log, check=False,
            )
            fetch_future = executor.submit(
                run_command,
                ["git", "fetch", "origin", base_branch],
                cwd=repo_dir, timeout=self.timeout, log=self.log,
            )
            existing = existing_future.result()
            fetch_future.result()
        if existing.returncode == 0:
            # Reusing the deterministic issue branch makes retries/resumed jobs
            # idempotent. The clean-worktree check above ensures this cannot
            # overwrite uncommitted local work.
            self.log(f"Remote branch already exists; resuming {branch_name}.")
            run_command(
                ["git", "fetch", "origin", branch_name],
                cwd=repo_dir, timeout=self.timeout, log=self.log,
            )
        run_command(["git", "checkout", "-B", branch_name, "FETCH_HEAD"], cwd=repo_dir, timeout=self.timeout, log=self.log)
        exclude = repo_dir / ".git" / "info" / "exclude"
        existing_excludes = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".ticket-agent/" not in existing_excludes.splitlines():
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write("\n.ticket-agent/\n")

    def _linked_open_prs(
        self, ref: IssueRef, issue_ref: IssueRef | None = None,
    ) -> list[dict]:
        """Return all open PRs in ``ref`` which explicitly link ``issue_ref``."""
        source = issue_ref or ref
        patterns = [
            rf"{re.escape(source.full_name)}#{source.number}(?!\d)",
            rf"github\.com/{re.escape(source.full_name)}/issues/{source.number}(?!\d)",
        ]
        if ref.full_name == source.full_name:
            patterns.append(rf"(?<!\d)#{source.number}(?!\d)")

        def links_ticket(pr: dict) -> bool:
            body = pr.get("body") or ""
            return any(re.search(pattern, body, re.IGNORECASE) for pattern in patterns)

        # GitHub's Development panel can attach a PR without adding an issue
        # reference to the PR body. closedByPullRequestsReferences is the
        # authoritative graph used by that UI and can span repositories.
        query = f'''query {{
          repository(owner: "{source.owner}", name: "{source.repo}") {{
            issue(number: {source.number}) {{
              closedByPullRequestsReferences(first: 50) {{ nodes {{
                number url state headRefName baseRefName body title
                repository {{ nameWithOwner }}
              }} }}
            }}
          }}
        }}'''
        with ThreadPoolExecutor(max_workers=2) as executor:
            listed_future = executor.submit(
                run_command,
                ["gh", "pr", "list", "--repo", ref.full_name, "--state", "open", "--limit", "100",
                 "--json", "number,url,headRefName,baseRefName,body,title"],
                timeout=self.timeout, log=self.log,
            )
            attached_future = executor.submit(
                run_command,
                ["gh", "api", "graphql", "-f", f"query={query}"],
                timeout=self.timeout, log=self.log, check=False,
            )
            result = listed_future.result()
            attached = attached_future.result()
        matches = [pr for pr in json.loads(result.stdout) if links_ticket(pr)]
        if getattr(attached, "returncode", 0) == 0:
            try:
                nodes = json.loads(attached.stdout)["data"]["repository"]["issue"]["closedByPullRequestsReferences"]["nodes"]
                matches.extend(
                    pr for pr in nodes
                    if pr.get("state") == "OPEN"
                    and (pr.get("repository") or {}).get("nameWithOwner") == ref.full_name
                )
            except (KeyError, TypeError, json.JSONDecodeError):
                pass

        unique: dict[str, dict] = {}
        for pr in matches:
            unique[str(pr.get("url") or pr.get("number"))] = pr
        return list(unique.values())

    def linked_open_pr(
        self, ref: IssueRef, issue_ref: IssueRef | None = None, *, required: bool = True,
    ) -> dict | None:
        """Find a single open PR in ``ref`` which explicitly links ``issue_ref``."""
        source = issue_ref or ref
        matches = self._linked_open_prs(ref, issue_ref)

        if not matches:
            if required:
                raise WorkflowError(f"No open pull request linked to ticket #{source.number} was found in {ref.full_name}.")
            return None
        if len(matches) > 1:
            raise WorkflowError(f"More than one open pull request in {ref.full_name} links ticket #{source.number}; close or unlink the obsolete PR first.")
        return matches[0]

    def checkout_pr_branch(self, repo_dir: Path, ref: IssueRef, pr: dict) -> None:
        if self.has_changes(repo_dir):
            raise WorkflowError("The target repository has uncommitted changes; the PR branch cannot be checked out safely.")
        run_command(["git", "fetch", "origin", pr["headRefName"]], cwd=repo_dir, timeout=self.timeout, log=self.log)
        run_command(["git", "checkout", "-B", pr["headRefName"], "FETCH_HEAD"], cwd=repo_dir, timeout=self.timeout, log=self.log)

    def checkout_base_branch(self, repo_dir: Path, base_branch: str) -> None:
        """Prepare a clean local base for inspecting a paired repository."""
        if self.has_changes(repo_dir):
            raise WorkflowError("The paired repository has uncommitted changes and cannot be prepared safely.")
        run_command(["git", "fetch", "origin", base_branch], cwd=repo_dir, timeout=self.timeout, log=self.log)
        run_command(["git", "checkout", "-B", base_branch, "FETCH_HEAD"], cwd=repo_dir, timeout=self.timeout, log=self.log)

    def ensure_commit_identity(self, repo_dir: Path) -> None:
        values = run_command(
            ["git", "config", "--get-regexp", r"^(user\.email|user\.name)$"],
            cwd=repo_dir, timeout=self.timeout, log=self.log, check=False,
        ).stdout.splitlines()
        configured = {
            key: value for line in values
            for key, _, value in [line.partition(" ")]
            if key and value
        }
        email = configured.get("user.email", "").strip()
        name = configured.get("user.name", "").strip()
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

    def discard_changes(self, repo_dir: Path) -> None:
        """Throw away all uncommitted work, including untracked files."""
        run_command(["git", "reset", "--hard"], cwd=repo_dir, timeout=self.timeout, log=self.log)
        run_command(["git", "clean", "-fd"], cwd=repo_dir, timeout=self.timeout, log=self.log)

    def has_changes(self, repo_dir: Path) -> bool:
        result = run_command(
            ["git", "status", "--porcelain"], cwd=repo_dir, timeout=self.timeout, log=self.log
        )
        return bool(result.stdout.strip())

    def current_branch(self, repo_dir: Path) -> str:
        return run_command(
            ["git", "branch", "--show-current"],
            cwd=repo_dir,
            timeout=self.timeout,
            log=self.log,
        ).stdout.strip()

    def diff(self, repo_dir: Path, base_branch: str) -> str:
        return run_command(
            ["git", "diff", f"origin/{base_branch}...HEAD"],
            cwd=repo_dir,
            timeout=self.timeout,
            log=self.log,
        ).stdout

    def changed_paths(self, repo_dir: Path, base_branch: str) -> list[str]:
        output = run_command(
            ["git", "diff", "--name-only", "-z", f"origin/{base_branch}...HEAD"],
            cwd=repo_dir, timeout=self.timeout, log=self.log,
        ).stdout
        return [path for path in output.split("\0") if path]

    def validate_diff(self, repo_dir: Path) -> None:
        run_command(["git", "diff", "--check"], cwd=repo_dir, timeout=self.timeout, log=self.log)

    def commit_and_push(
        self, repo_dir: Path, branch_name: str, commit_message: str, expected_repository: str
    ) -> None:
        config_lines = run_command(
            [
                "git", "config", "--get-regexp",
                r"^(remote\.origin\.url|user\.email|user\.name)$",
            ],
            cwd=repo_dir, timeout=self.timeout, log=self.log, check=False,
        ).stdout.splitlines()
        configured = {
            key: value for line in config_lines
            for key, _, value in [line.partition(" ")]
            if key and value
        }
        origin = configured.get("remote.origin.url", "").strip()
        accepted = {
            f"https://github.com/{expected_repository}.git",
            f"https://github.com/{expected_repository}",
            f"git@github.com:{expected_repository}.git",
        }
        if origin not in accepted:
            raise WorkflowError(f"Origin remote changed unexpectedly: {origin}")
        safe_hooks = repo_dir.parent / "empty-git-hooks"
        safe_hooks.mkdir(exist_ok=True)
        if not configured.get("user.email"):
            run_command(
                ["git", "config", "user.email", "ticket-agent@localhost"],
                cwd=repo_dir, timeout=self.timeout, log=self.log,
            )
        if not configured.get("user.name"):
            run_command(
                ["git", "config", "user.name", "Ticket PR Agent"],
                cwd=repo_dir, timeout=self.timeout, log=self.log,
            )
        run_command(["git", "add", "-A"], cwd=repo_dir, timeout=self.timeout, log=self.log)
        staged = run_command(
            ["git", "diff", "--cached", "--name-only", "-z"],
            cwd=repo_dir, timeout=self.timeout, log=self.log,
        ).stdout.split("\0")
        test_files = [path for path in staged if path and is_test_file(path)]
        if test_files:
            run_command(
                ["git", "restore", "--staged", "--", *test_files],
                cwd=repo_dir, timeout=self.timeout, log=self.log,
            )
            self.log(f"Excluded {len(test_files)} test file(s) from the pull request.")
        if not any(path and path not in test_files for path in staged):
            raise WorkflowError("No non-test changes remain to include in the pull request.")
        run_command(
            ["git", "-c", f"core.hooksPath={safe_hooks}", "commit", "-m", commit_message],
            cwd=repo_dir, timeout=self.timeout, log=self.log,
        )

        # A prior or partially completed delivery may already have published the
        # same ticket branch. Preserve that remote work and rebase the new commit
        # onto it instead of failing with a non-fast-forward error or force-pushing.
        pushed = run_command(
            ["git", "push", "--set-upstream", "origin", branch_name],
            cwd=repo_dir,
            timeout=self.timeout,
            log=self.log,
            check=False,
        )
        if pushed.returncode == 0:
            return
        self.log(
            f"Initial push of `{branch_name}` was rejected; checking for resumable remote work."
        )
        run_command(
            ["git", "fetch", "origin", branch_name],
            cwd=repo_dir, timeout=self.timeout, log=self.log,
        )
        fast_forward = run_command(
            ["git", "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"],
            cwd=repo_dir, timeout=self.timeout, log=self.log, check=False,
        )
        if fast_forward.returncode != 0:
            self.log(
                f"Remote ticket branch `{branch_name}` advanced; rebasing the reviewed commit "
                "before publication."
            )
            run_command(
                ["git", "rebase", "FETCH_HEAD"],
                cwd=repo_dir, timeout=self.timeout, log=self.log,
            )
        run_command(
            ["git", "push", "--set-upstream", "origin", branch_name],
            cwd=repo_dir, timeout=self.timeout, log=self.log,
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

    def assign_issue(self, ref: IssueRef, login: str) -> None:
        run_command(
            ["gh", "issue", "edit", str(ref.number), "--repo", ref.full_name, "--add-assignee", login],
            timeout=self.timeout,
            log=self.log,
            check=False,
        )

    def assign_pr(self, ref: IssueRef, pr_url: str, login: str) -> None:
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        run_command(
            ["gh", "pr", "edit", str(pr_number), "--repo", ref.full_name, "--add-assignee", login],
            timeout=self.timeout,
            log=self.log,
            check=False,
        )

    def diff_stat(self, repo_dir: Path, base_branch: str) -> tuple[int, int]:
        result = run_command(
            ["git", "diff", "--numstat", f"origin/{base_branch}...HEAD"],
            cwd=repo_dir,
            timeout=self.timeout,
            log=self.log,
            check=False,
        )
        additions = deletions = 0
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                additions += int(parts[0])
                deletions += int(parts[1])
        return additions, deletions

    def diff_summary(self, repo_dir: Path, base_branch: str) -> tuple[list[str], int, int]:
        """Return changed paths and line totals from one git process."""
        result = run_command(
            ["git", "diff", "--numstat", "-z", f"origin/{base_branch}...HEAD"],
            cwd=repo_dir,
            timeout=self.timeout,
            log=self.log,
            check=False,
        )
        records = result.stdout.split("\0")
        paths: list[str] = []
        additions = deletions = 0
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            parts = record.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, path = parts
            if added.isdigit():
                additions += int(added)
            if removed.isdigit():
                deletions += int(removed)
            if not path and index + 1 < len(records):
                # With -z, rename/copy records put old and new names in the next
                # two NUL-delimited fields. The destination is the changed path.
                index += 1
                path = records[index]
                index += 1
            if path:
                paths.append(path)
        return paths, additions, deletions

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
