from types import SimpleNamespace

import github_ops
import pytest
from core import WorkflowError
from github_ops import GitHubOps
from workflow import WorkflowRunner


def test_current_branch_reports_resume_provenance(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout="bug-fix/202-project-create-unable-to-create-a-matrix-project\n",
        )

    monkeypatch.setattr(github_ops, "run_command", fake_run)

    branch = GitHubOps(10, lambda message: None).current_branch(tmp_path / "crm-api")

    assert branch == "bug-fix/202-project-create-unable-to-create-a-matrix-project"
    assert calls == [["git", "branch", "--show-current"]]


def test_same_ticket_branch_is_resumed_but_other_branch_conflicts(tmp_path):
    expected = "bug-fix/202-project-create-unable-to-create-a-matrix-project"

    class FakeGitHub:
        def has_changes(self, repo_dir):
            return True

        def current_branch(self, repo_dir):
            return expected if repo_dir.name == "crm-api" else "bug-fix/99-other-ticket"

    logs = []
    resumable, conflicts = WorkflowRunner._classify_dirty_repositories(
        {
            "crm-api": tmp_path / "crm-api",
            "crm-staff-desktop": tmp_path / "crm-staff-desktop",
        },
        expected,
        FakeGitHub(),
        logs.append,
    )

    assert resumable == {"crm-api"}
    assert conflicts == ["crm-staff-desktop"]
    assert logs == [f"Resuming existing ticket changes in crm-api on `{expected}`."]


def test_discard_changes_resets_and_cleans_the_repository(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(github_ops, "run_command", fake_run)
    GitHubOps(10, lambda message: None).discard_changes(tmp_path / "crm-api")

    assert calls == [["git", "reset", "--hard"], ["git", "clean", "-fd"]]


def test_new_contract_rejects_dirty_repository_before_switching_branch(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=" M existing-change.ts\n")

    monkeypatch.setattr(github_ops, "run_command", fake_run)

    with pytest.raises(WorkflowError, match="uncommitted changes from another operation"):
        GitHubOps(10, lambda message: None).prepare_branch(
            tmp_path / "crm-api", "develop", "bug-fix/7-fix-ticket",
        )

    assert calls == [["git", "status", "--porcelain"]]


def test_clean_repository_prepares_contract_branch(monkeypatch, tmp_path):
    calls = []
    repo_dir = tmp_path / "crm-api"
    (repo_dir / ".git" / "info").mkdir(parents=True)

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="")
        if args[0:3] == ["git", "ls-remote", "--exit-code"]:
            return SimpleNamespace(returncode=2, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(github_ops, "run_command", fake_run)
    GitHubOps(10, lambda message: None).prepare_branch(
        repo_dir, "develop", "bug-fix/7-fix-ticket",
    )

    assert calls[-1] == ["git", "checkout", "-B", "bug-fix/7-fix-ticket", "FETCH_HEAD"]


def test_existing_remote_branch_is_resumed(monkeypatch, tmp_path):
    calls = []
    repo_dir = tmp_path / "crm-api"
    (repo_dir / ".git" / "info").mkdir(parents=True)

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="")
        if args[0:3] == ["git", "ls-remote", "--exit-code"]:
            return SimpleNamespace(returncode=0, stdout="commit refs/heads/bug-fix/7-fix-ticket\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(github_ops, "run_command", fake_run)
    GitHubOps(10, lambda message: None).prepare_branch(
        repo_dir, "develop", "bug-fix/7-fix-ticket",
    )

    assert ["git", "fetch", "origin", "bug-fix/7-fix-ticket"] in calls
    assert calls[-1] == ["git", "checkout", "-B", "bug-fix/7-fix-ticket", "FETCH_HEAD"]
