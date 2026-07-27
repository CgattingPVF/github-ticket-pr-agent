from pathlib import Path

import github_ops
from core import CommandResult
from github_ops import GitHubOps


def test_existing_remote_ticket_branch_is_rebased_before_push(monkeypatch, tmp_path: Path):
    commands: list[list[str]] = []
    push_attempts = 0

    def fake_run(args, **kwargs):
        nonlocal push_attempts
        commands.append(args)
        stdout = ""
        returncode = 0
        if args[:3] == ["git", "config", "--get-regexp"]:
            stdout = (
                "remote.origin.url https://github.com/acme/widgets.git\n"
                "user.email agent@example.com\nuser.name MergeQuest\n"
            )
        elif args == ["git", "diff", "--cached", "--name-only", "-z"]:
            stdout = "src/widget.py\0"
        elif args[:3] == ["git", "push", "--set-upstream"]:
            push_attempts += 1
            returncode = 1 if push_attempts == 1 else 0
        elif args[:3] == ["git", "merge-base", "--is-ancestor"]:
            returncode = 1
        return CommandResult(args=args, returncode=returncode, stdout=stdout)

    monkeypatch.setattr(github_ops, "run_command", fake_run)
    GitHubOps(30, lambda message: None).commit_and_push(
        tmp_path, "bug-fix/42-widgets", "Fix widgets", "acme/widgets"
    )

    assert ["git", "fetch", "origin", "bug-fix/42-widgets"] in commands
    assert ["git", "rebase", "FETCH_HEAD"] in commands
    assert commands.count(["git", "push", "--set-upstream", "origin", "bug-fix/42-widgets"]) == 2
