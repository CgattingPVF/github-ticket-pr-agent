from types import SimpleNamespace

import workflow
from workflow import WorkflowRunner


class FakeTestingStore:
    def __init__(self, issue_url):
        self.job = {
            "id": "qa123",
            "status": "queued",
            "parameters": {
                "issue_url": issue_url,
                "agent_provider": "codex",
                "agent_command": "",
                "workflow_profile": "testing_only",
            },
        }
        self.logs = []

    def get(self, job_id):
        return self.job if job_id == "qa123" else None

    def update(self, job_id, **fields):
        result = fields.pop("result_json", None)
        self.job.update(fields)
        if result is not None:
            self.job["result"] = result

    def append_log(self, job_id, message):
        self.logs.append(message)


class FakeTestingGitHub:
    def __init__(self):
        self.transition_calls = []
        self.checked_out = []

    def check_auth(self):
        pass

    def get_issue(self, ref, *, require_open=True):
        return {
            "number": ref.number,
            "title": "Keep an open PR ready",
            "body": "",
            "labels": [],
            "state": "open",
        }

    def get_compact_issue_context(self, ref):
        return {"latest_comments": [], "linked_pull_requests": []}

    def linked_open_pr(self, ref, issue_ref=None, required=True):
        return {
            "number": 70 if ref.repo == "crm-staff-desktop" else 71,
            "url": f"https://github.com/acme/{ref.repo}/pull/7",
            "headRefName": f"feature/7-{ref.repo}",
            "baseRefName": "develop",
        }

    def checkout_pr_branch(self, repo_dir, ref, pr):
        self.checked_out.append((repo_dir.name, pr["headRefName"]))

    def has_changes(self, repo_dir):
        return False

    def comment_on_issue(self, ref, body, artifact_dir):
        pass

    def sync_successful_qa_project_fields(self, ref, repositories):
        self.transition_calls.append((ref, repositories))
        return {
            "updated": True,
            "count": 1,
            "test_state_count": 1,
            "status": "PR Ready",
            "test_state": None,
            "has_open_pr": True,
        }


def testing_settings(workspace, tmp_path, **overrides):
    defaults = dict(
        local_repo_path=workspace,
        workspace_root=tmp_path / "artifacts",
        command_timeout_seconds=10,
        agent_command="codex exec -c 'model=\"gpt-5.6-luna\"' -",
        review_command="review",
        claude_command="claude -p --model claude-haiku-4-5",
        editor_command="code",
        max_gate_attempts=15,
        testing_pass_timeout_seconds=3,
        testing_max_attempts=3,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_integrity_scanner_checks_both_repositories_before_project_transition(
    monkeypatch, tmp_path,
):
    workspace = tmp_path / "CRM_APP_PVF"
    for name in ("crm-staff-desktop", "crm-api"):
        (workspace / name / ".git").mkdir(parents=True)

    store = FakeTestingStore(
        "https://github.com/acme/crm-staff-desktop/issues/7",
    )
    settings = testing_settings(
        workspace, tmp_path,
        agent_command="agent",
        claude_command="claude",
        max_gate_attempts=6,
    )
    qa_timeouts = []
    notifications = []
    fake_github = FakeTestingGitHub()

    monkeypatch.setattr(workflow, "GitHubOps", lambda *args, **kwargs: fake_github)
    monkeypatch.setattr(workflow, "command_exists", lambda command: True)
    def fake_run_configured_command(*args, **kwargs):
        qa_timeouts.append(kwargs["timeout"])

    monkeypatch.setattr(workflow, "run_configured_command", fake_run_configured_command)
    monkeypatch.setattr(
        workflow,
        "load_json",
        lambda path: {
            "summary": "All automated checks passed.",
            "overall": "passed",
            "tests_run": [{"command": "pytest", "result": "passed"}],
        },
    )
    monkeypatch.setattr(workflow, "working_tree_fingerprint", lambda path: path.name)
    monkeypatch.setattr(
        workflow, "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout="develop\n", returncode=0),
    )

    runner = WorkflowRunner(settings, store)
    monkeypatch.setattr(runner, "_open_editor", lambda path, log: None)
    monkeypatch.setattr(
        runner,
        "_send_windows_notification",
        lambda title, body, log, launch_url=None: notifications.append(title),
    )
    runner.run_testing("qa123")

    assert store.job["status"] == "completed"
    assert store.job["result"]["repositories"] == ["crm-staff-desktop", "crm-api"]
    assert store.job["result"]["project_status"]["status"] == "PR Ready"
    assert store.job["result"]["project_status"]["test_state"] is None
    assert fake_github.transition_calls[0][1] == ["crm-staff-desktop", "crm-api"]
    assert fake_github.checked_out == [
        ("crm-staff-desktop", "feature/7-crm-staff-desktop"),
        ("crm-api", "feature/7-crm-api"),
    ]
    assert set(store.job["result"]["tested_prs"]) == {"crm-staff-desktop", "crm-api"}
    assert qa_timeouts == [3]
    assert notifications == ["MergeQuest: Testing PASS #7"]


def test_integrity_scanner_uses_testing_attempt_limit(monkeypatch, tmp_path):
    workspace = tmp_path / "CRM_APP_PVF"
    (workspace / "crm-staff-desktop" / ".git").mkdir(parents=True)
    store = FakeTestingStore("https://github.com/acme/crm-staff-desktop/issues/7")
    settings = testing_settings(workspace, tmp_path, max_gate_attempts=15, testing_max_attempts=3)
    fake_github = FakeTestingGitHub()
    attempts = []
    notifications = []

    monkeypatch.setattr(workflow, "GitHubOps", lambda *args, **kwargs: fake_github)
    monkeypatch.setattr(workflow, "command_exists", lambda command: True)
    monkeypatch.setattr(workflow, "working_tree_fingerprint", lambda path: path.name)
    monkeypatch.setattr(
        workflow, "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout="develop\n", returncode=0),
    )

    def fail_qa(command, **kwargs):
        attempts.append(command)
        raise workflow.WorkflowError("provider stalled")

    monkeypatch.setattr(workflow, "run_configured_command", fail_qa)

    runner = WorkflowRunner(settings, store)
    monkeypatch.setattr(runner, "_open_editor", lambda path, log: None)
    monkeypatch.setattr(
        runner,
        "_send_windows_notification",
        lambda title, body, log, launch_url=None: notifications.append(title),
    )
    runner.run_testing("qa123")

    assert len(attempts) == 3
    assert store.job["status"] == "failed"
    assert "provider stalled" in store.job["error"]
    assert any("QA attempt 3/3" in message for message in store.logs)
    assert notifications == ["MergeQuest: Testing FAIL #7"]


def test_integrity_scanner_failed_report_notifies_fail(monkeypatch, tmp_path):
    workspace = tmp_path / "CRM_APP_PVF"
    (workspace / "crm-staff-desktop" / ".git").mkdir(parents=True)
    store = FakeTestingStore("https://github.com/acme/crm-staff-desktop/issues/7")
    settings = testing_settings(workspace, tmp_path)
    fake_github = FakeTestingGitHub()
    notifications = []

    monkeypatch.setattr(workflow, "GitHubOps", lambda *args, **kwargs: fake_github)
    monkeypatch.setattr(workflow, "command_exists", lambda command: True)
    monkeypatch.setattr(workflow, "run_configured_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        workflow,
        "load_json",
        lambda path: {
            "summary": "Focused check failed.",
            "overall": "failed",
            "tests_run": [{"command": "pytest", "result": "failed"}],
        },
    )
    monkeypatch.setattr(workflow, "working_tree_fingerprint", lambda path: path.name)
    monkeypatch.setattr(
        workflow, "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout="develop\n", returncode=0),
    )

    runner = WorkflowRunner(settings, store)
    monkeypatch.setattr(runner, "_open_editor", lambda path, log: None)
    monkeypatch.setattr(
        runner,
        "_send_windows_notification",
        lambda title, body, log, launch_url=None: notifications.append(title),
    )
    runner.run_testing("qa123")

    assert store.job["status"] == "completed"
    assert store.job["result"]["overall"] == "failed"
    assert notifications == ["MergeQuest: Testing FAIL #7"]


def test_integrity_scanner_recovers_text_verdict_when_json_report_missing(monkeypatch, tmp_path):
    workspace = tmp_path / "CRM_APP_PVF"
    (workspace / "crm-staff-desktop" / ".git").mkdir(parents=True)
    store = FakeTestingStore("https://github.com/acme/crm-staff-desktop/issues/7")
    settings = testing_settings(workspace, tmp_path)
    fake_github = FakeTestingGitHub()
    notifications = []

    monkeypatch.setattr(workflow, "GitHubOps", lambda *args, **kwargs: fake_github)
    monkeypatch.setattr(workflow, "command_exists", lambda command: True)
    monkeypatch.setattr(workflow, "working_tree_fingerprint", lambda path: path.name)
    monkeypatch.setattr(
        workflow, "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout="develop\n", returncode=0),
    )

    def text_only_qa(command, *, log, **kwargs):
        log(
            "QA result: **FAIL / incomplete**\n\n"
            "Evidence:\n\n"
            "- CompanyFilterDrawer.vue marks several filters as Coming soon.\n"
            "- app/stores/company.ts applies only categoryIds and accountsAuthorised.\n"
            "No production files were edited."
        )

    monkeypatch.setattr(workflow, "run_configured_command", text_only_qa)

    runner = WorkflowRunner(settings, store)
    monkeypatch.setattr(runner, "_open_editor", lambda path, log: None)
    monkeypatch.setattr(
        runner,
        "_send_windows_notification",
        lambda title, body, log, launch_url=None: notifications.append(title),
    )
    runner.run_testing("qa123")

    assert store.job["status"] == "completed"
    assert store.job["result"]["overall"] == "failed"
    assert store.job["result"]["tests_run"][0]["result"] == "failed"
    assert "CompanyFilterDrawer.vue" in store.job["result"]["summary"]
    assert notifications == ["MergeQuest: Testing FAIL #7"]
    assert any("Recovered QA verdict from provider telemetry" in message for message in store.logs)


def test_integrity_scanner_uses_report_written_before_timeout(monkeypatch, tmp_path):
    workspace = tmp_path / "CRM_APP_PVF"
    (workspace / "crm-staff-desktop" / ".git").mkdir(parents=True)
    store = FakeTestingStore("https://github.com/acme/crm-staff-desktop/issues/7")
    settings = testing_settings(workspace, tmp_path)
    fake_github = FakeTestingGitHub()
    attempts = []

    monkeypatch.setattr(workflow, "GitHubOps", lambda *args, **kwargs: fake_github)
    monkeypatch.setattr(workflow, "command_exists", lambda command: True)
    monkeypatch.setattr(workflow, "working_tree_fingerprint", lambda path: path.name)
    monkeypatch.setattr(
        workflow, "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout="develop\n", returncode=0),
    )

    def write_then_timeout(command, *, cwd, **kwargs):
        attempts.append(command)
        (cwd / ".ticket-agent" / "qa-qa123.json").write_text(
            '{"summary":"Priority filter evidence passed.","overall":"passed","tests_run":[{"command":"bun test","result":"passed"}]}',
            encoding="utf-8",
        )
        raise workflow.WorkflowError("Command timed out after 600 seconds")

    monkeypatch.setattr(workflow, "run_configured_command", write_then_timeout)

    runner = WorkflowRunner(settings, store)
    monkeypatch.setattr(runner, "_open_editor", lambda path, log: None)
    monkeypatch.setattr(runner, "_send_windows_notification", lambda *args, **kwargs: None)
    runner.run_testing("qa123")

    assert len(attempts) == 1
    assert store.job["status"] == "completed"
    assert store.job["result"]["overall"] == "passed"
    assert any("using the completed report instead of escalating" in message for message in store.logs)


def test_integrity_scanner_hands_prior_work_to_escalated_model(monkeypatch, tmp_path):
    workspace = tmp_path / "CRM_APP_PVF"
    (workspace / "crm-staff-desktop" / ".git").mkdir(parents=True)
    store = FakeTestingStore("https://github.com/acme/crm-staff-desktop/issues/7")
    settings = testing_settings(workspace, tmp_path, testing_max_attempts=2)
    fake_github = FakeTestingGitHub()
    prompts = []

    monkeypatch.setattr(workflow, "GitHubOps", lambda *args, **kwargs: fake_github)
    monkeypatch.setattr(workflow, "command_exists", lambda command: True)
    monkeypatch.setattr(workflow, "working_tree_fingerprint", lambda path: path.name)
    monkeypatch.setattr(
        workflow, "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout="develop\n", returncode=0),
    )

    def run_qa(command, *, cwd, prompt, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            kwargs["log"]("checked quote-status/staff.ts and found the Lost mapping")
            raise workflow.WorkflowError("provider stalled")
        (cwd / ".ticket-agent" / "qa-qa123.json").write_text(
            '{"summary":"continued prior trail","overall":"passed","tests_run":[{"command":"pytest","result":"passed","notes":"ok"}]}',
            encoding="utf-8",
        )

    monkeypatch.setattr(workflow, "run_configured_command", run_qa)

    runner = WorkflowRunner(settings, store)
    monkeypatch.setattr(runner, "_open_editor", lambda path, log: None)
    runner.run_testing("qa123")

    assert store.job["status"] == "completed"
    assert len(prompts) == 2
    assert "ESCALATION HANDOFF" in prompts[1]
    assert "checked quote-status/staff.ts" in prompts[1]
    assert "provider stalled" in prompts[1]
