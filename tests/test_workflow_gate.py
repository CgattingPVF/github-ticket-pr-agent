import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

import workflow
from core import parse_issue_url
from workflow import WorkflowRunner


class FakeStore:
    def get(self, job_id):
        return {"status": "running", "parameters": {}}

    def append_log(self, job_id, message):
        pass

    def update(self, job_id, **fields):
        pass


class FakeSettings:
    command_timeout_seconds = 10
    minimum_confidence = 0.90
    max_gate_attempts = 6
    agent_command = "codex exec -c 'model=\"gpt-5.6-luna\"' -"
    claude_command = "claude -p --model claude-haiku-4-5"


def _result(confidence):
    return {
        "safe_to_pr": True, "confidence": confidence, "summary": "s",
        "root_cause": "r", "tests_run": [{"command": "t", "result": "passed"}],
        "unresolved_risks": [], "commit_message": "m", "pr_title": "p",
    }


def test_stage_confidence_moves_directionally_through_delivery():
    assert WorkflowRunner._stage_confidence("Investigating and implementing") == 0.52
    assert WorkflowRunner._stage_confidence("Running validation") == 0.68
    assert WorkflowRunner._stage_confidence("Repairing review findings (1/2)") == 0.48
    assert WorkflowRunner._stage_confidence("Completed") == 1.0


def test_live_confidence_is_published_without_discarding_result_metadata():
    class Store(FakeStore):
        def __init__(self):
            self.result = {"summary": "working", "confidence": 0.2}

        def get(self, job_id):
            return {"status": "running", "parameters": {}, "result": self.result}

        def update(self, job_id, **fields):
            self.result = fields["result_json"]

    store = Store()
    runner = WorkflowRunner(FakeSettings(), store)
    runner.publish_agent_confidence("job", 0.84, "Agent assessment")
    assert store.result["summary"] == "working"
    assert store.result["confidence"] == 0.84
    assert store.result["confidence_live"] is True


def test_prompt_tokens_accumulate_across_session_passes():
    class Store(FakeStore):
        def __init__(self):
            self.result = {"session_output_tokens": 7}

        def get(self, job_id):
            return {"status": "running", "parameters": {}, "result": self.result}

        def update(self, job_id, **fields):
            self.result = fields["result_json"]

    store = Store()
    runner = WorkflowRunner(FakeSettings(), store)

    runner._publish_prompt_tokens("job", "a" * 40)
    runner._publish_prompt_tokens("job", "b" * 20)

    assert store.result["prompt_tokens"] == 5
    assert store.result["session_prompt_tokens"] == 15
    assert store.result["session_tokens"] == 22


def test_gate_loop_retries_until_confident(monkeypatch, tmp_path):
    results = iter([_result(0.5), _result(0.7), _result(0.95)])
    calls = []
    monkeypatch.setattr(workflow, "run_configured_command", lambda *a, **k: calls.append(k.get("prompt")))
    monkeypatch.setattr(workflow, "load_json", lambda path: next(results))

    runner = WorkflowRunner(FakeSettings(), FakeStore())
    result = runner._run_agent_gated(
        "job", "agent", tmp_path, tmp_path / "result.json", {"number": 1}, "go", lambda m: None
    )
    assert result["confidence"] == 0.95
    assert len(calls) == 3
    assert "gate rejected" in calls[1]


def test_low_confidence_retry_promotes_the_agent_model(monkeypatch, tmp_path):
    results = iter([_result(0.72)] * 5 + [_result(0.95)])
    commands = []
    monkeypatch.setattr(
        workflow, "run_configured_command",
        lambda command, **kwargs: commands.append(command),
    )
    monkeypatch.setattr(workflow, "load_json", lambda path: next(results))
    runner = WorkflowRunner(FakeSettings(), FakeStore())

    runner._run_agent_gated(
        "job",
        "codex exec -c 'model=\"gpt-5.6-luna\"' -c 'model_reasoning_effort=\"low\"' -",
        tmp_path,
        tmp_path / "result.json",
        {"number": 1},
        "go",
        lambda message: None,
    )

    assert "gpt-5.6-luna" in commands[0]
    assert all("gpt-5.6-luna" in command for command in commands[:5])
    assert "claude-opus-4-7" in commands[5]
    assert "model_reasoning_effort=\"low\"" in commands[1]


def test_low_confidence_retry_promotes_the_claude_model(monkeypatch, tmp_path):
    results = iter([_result(0.72)] * 5 + [_result(0.95)])
    commands = []
    monkeypatch.setattr(
        workflow, "run_configured_command",
        lambda command, **kwargs: commands.append(command),
    )
    monkeypatch.setattr(workflow, "load_json", lambda path: next(results))
    runner = WorkflowRunner(FakeSettings(), FakeStore())

    runner._run_agent_gated(
        "job",
        "claude -p --model claude-haiku-4-5 --output-format stream-json --verbose",
        tmp_path,
        tmp_path / "result.json",
        {"number": 1},
        "go",
        lambda message: None,
    )

    assert "--model claude-haiku-4-5" in commands[0]
    assert all("--model claude-haiku-4-5" in command for command in commands[:5])
    assert "--model claude-sonnet-5" in commands[5]


def test_agent_command_crash_escalates_model_and_retries(monkeypatch, tmp_path):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            raise workflow.WorkflowError("Command failed with exit code 1: codex")

    monkeypatch.setattr(workflow, "run_configured_command", fake_run)
    monkeypatch.setattr(workflow, "load_json", lambda path: _result(0.95))
    runner = WorkflowRunner(FakeSettings(), FakeStore())

    result = runner._run_agent_gated(
        "job",
        "codex exec -c 'model=\"gpt-5.6-luna\"' -c 'model_reasoning_effort=\"low\"' -",
        tmp_path,
        tmp_path / "result.json",
        {"number": 1},
        "go",
        lambda message: None,
    )

    assert result["confidence"] == 0.95
    assert "gpt-5.6-luna" in commands[0]
    assert "gpt-5.6-luna" in commands[1]


def test_gate_loop_retries_on_missing_fields(monkeypatch, tmp_path):
    results = iter([{"confidence": 0.95}, _result(0.95)])
    calls = []
    monkeypatch.setattr(workflow, "run_configured_command", lambda *a, **k: calls.append(k.get("prompt")))
    monkeypatch.setattr(workflow, "load_json", lambda path: next(results))

    runner = WorkflowRunner(FakeSettings(), FakeStore())
    result = runner._run_agent_gated(
        "job", "agent", tmp_path, tmp_path / "result.json", {"number": 1}, "go", lambda m: None
    )
    assert result["pr_title"] == "p"
    assert "missing required fields" in calls[1]


def test_gate_loop_stops_after_max_attempts(monkeypatch, tmp_path):
    """A ticket that never converges (e.g. legitimate unresolved MEDIUM risks
    the agent won't clear) must terminate, not spin forever."""
    stuck = {**_result(0.86), "unresolved_risks": ["a medium risk that stays"]}
    calls = []
    monkeypatch.setattr(workflow, "run_configured_command", lambda *a, **k: calls.append(k.get("prompt")))
    monkeypatch.setattr(workflow, "load_json", lambda path: dict(stuck))

    runner = WorkflowRunner(FakeSettings(), FakeStore())
    try:
        runner._run_agent_gated(
            "job", "agent", tmp_path, tmp_path / "result.json", {"number": 1}, "go", lambda m: None
        )
        assert False, "expected WorkflowError"
    except workflow.WorkflowError:
        pass
    assert len(calls) == FakeSettings.max_gate_attempts


def test_fix_retest_gate_retries_skipped_checks_until_everything_passes(monkeypatch, tmp_path):
    skipped = {**_result(0.95), "tests_run": [{"command": "ui proof", "result": "not-run"}]}
    results = iter([skipped, _result(0.95)])
    calls = []
    monkeypatch.setattr(workflow, "run_configured_command", lambda *a, **k: calls.append(k.get("prompt")))
    monkeypatch.setattr(workflow, "load_json", lambda path: next(results))

    runner = WorkflowRunner(FakeSettings(), FakeStore())
    result = runner._run_agent_gated(
        "job", "agent", tmp_path, tmp_path / "result.json", {"number": 1}, "go", lambda m: None,
        require_all_tests_passed=True,
    )

    assert result["tests_run"][0]["result"] == "passed"
    assert len(calls) == 2
    assert "100% pass rate" in calls[1]


def test_manual_ui_skip_is_excluded_from_automated_qa_evidence():
    manual = {
        "command": "UI interaction against the Companies page",
        "result": "skipped",
        "notes": "No Windows desktop session or running app instance is available.",
    }
    relevant = {
        "command": "Database integration fixture",
        "result": "skipped",
        "notes": "Test database is unavailable.",
    }

    assert WorkflowRunner._is_manual_ui_skip(manual) is True
    assert WorkflowRunner._is_manual_ui_skip(relevant) is False


def test_autonomous_frontend_frameworks_are_rejected():
    assert WorkflowRunner._is_autonomous_frontend_test({"command": "npx playwright test"}) is True
    assert WorkflowRunner._is_autonomous_frontend_test({"command": "pytest tests/api"}) is False


def test_qa_outcome_passes_when_executed_checks_pass_and_others_skip():
    assert WorkflowRunner._qa_outcome(["passed", "skipped", "not-run"]) == "passed"
    assert WorkflowRunner._qa_outcome(["skipped", "not-run"]) == "incomplete"
    assert WorkflowRunner._qa_outcome(["passed", "failed", "skipped"]) == "failed"


def test_pr_body_static_builder_handles_reported_tests():
    result = {
        **_result(0.95),
        "evidence": ["src/widgets.py:42 proves duplicate insertion"],
        "files_changed": ["src/widgets.py"],
    }
    body = WorkflowRunner._build_pr_body(
        result,
        "## Automated review\n\nPASS",
        "Fixes",
        "acme/widgets#42",
        "main",
        "main",
    )

    assert "`t`: **passed**" in body
    assert "`src/widgets.py`" in body
    assert "src/widgets.py:42 proves duplicate insertion" in body
    assert "Fixes acme/widgets#42" in body


def test_integrity_checks_return_exact_pr_evidence(monkeypatch, tmp_path):
    calls = []

    class FakeGitHub:
        def validate_diff(self, repo_dir):
            calls.append(("diff", repo_dir))

    monkeypatch.setattr(
        workflow,
        "run_command",
        lambda command, **kwargs: calls.append((command, kwargs["cwd"])),
    )
    runner = WorkflowRunner(FakeSettings(), FakeStore())

    checks = runner._run_integrity_checks(
        {"widgets": tmp_path}, [["python", "-m", "pytest", "-q"]], FakeGitHub(), lambda message: None,
    )

    assert calls == [
        ("diff", tmp_path),
        (["python", "-m", "pytest", "-q"], tmp_path),
    ]
    assert checks == [
        {
            "command": "git diff --check (widgets)",
            "result": "passed",
            "notes": "No whitespace errors or conflict markers were found in the proposed diff.",
            "repository": "widgets",
        },
        {
            "command": "python -m pytest -q (widgets)",
            "result": "passed",
            "notes": "Configured Integrity command completed successfully.",
            "repository": "widgets",
        },
    ]


def test_finish_pr_updates_existing_branch_without_creating_another_pr(tmp_path):
    pushes = []

    class ExistingPrGitHub:
        def get_repository(self, ref):
            return {"default_branch": "main"}

        def commit_and_push(self, repo_dir, branch_name, commit_message, expected_repository):
            pushes.append((branch_name, expected_repository))

        def create_pr(self, *args, **kwargs):
            raise AssertionError("fix/retest must not create a PR")

        def post_review(self, *args, **kwargs):
            pass

        def comment_on_issue(self, *args, **kwargs):
            pass

        def changed_paths(self, *args, **kwargs):
            return []

        def diff_stat(self, *args, **kwargs):
            return (1, 0)

        def diff_summary(self, *args, **kwargs):
            return ([], 1, 0)

    runner = WorkflowRunner(FakeSettings(), FakeStore())
    ref = parse_issue_url("https://github.com/acme/widgets/issues/42")
    pr = {
        "url": "https://github.com/acme/widgets/pull/9",
        "headRefName": "bug-fix/42-existing",
        "baseRefName": "main",
    }
    review = {"verdict": "PASS", "summary": "clean", "findings": []}
    runner._finish_pr(
        "job", {}, lambda message: None, ref,
        {"html_url": "https://github.com/acme/widgets/issues/42"},
        "main", "unused-new-branch", {"widgets": tmp_path}, _result(0.95), review,
        {"widgets": review}, None, ExistingPrGitHub(), tmp_path, time.time(),
        existing_prs={"widgets": pr},
    )

    assert pushes == [("bug-fix/42-existing", "acme/widgets")]


def test_finish_pr_refuses_to_push_below_confidence_threshold(tmp_path):
    runner = WorkflowRunner(FakeSettings(), FakeStore())
    ref = parse_issue_url("https://github.com/acme/widgets/issues/42")
    try:
        runner._finish_pr(
            "job", {}, lambda message: None, ref,
            {"html_url": "https://github.com/acme/widgets/issues/42"},
            "main", "bug-fix/42", {"widgets": tmp_path}, _result(0.89),
            {"verdict": "PASS", "summary": "clean", "findings": []},
            {"widgets": {"verdict": "PASS", "summary": "clean", "findings": []}},
            None, object(), tmp_path, time.time(),
        )
        assert False, "expected low-confidence publication to be rejected"
    except workflow.WorkflowError as exc:
        assert "Model escalation must complete first" in str(exc)


def test_pr_notification_uses_windows_notification(monkeypatch):
    sent = []

    def fake_run(*args, **kwargs):
        sent.append(args[0])
        return subprocess.CompletedProcess(args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workflow.os, "name", "nt")
    monkeypatch.setattr(workflow.shutil, "which", lambda name: name)
    monkeypatch.setattr(workflow.subprocess, "run", fake_run)
    logs = []
    runner = WorkflowRunner(FakeSettings(), FakeStore())
    runner._send_pr_notification(
        {"number": 42, "title": "Fix widgets", "html_url": "https://github.com/acme/widgets/issues/42"},
        {"summary": "Fixed widgets", "confidence": 0.95},
        {"widgets": "https://github.com/acme/widgets/pull/9"},
        logs.append,
    )

    assert len(sent) == 1
    assert sent[0][0:2] == ["powershell.exe", "-NoProfile"]
    assert "#42" in sent[0][3]
    assert "https://github.com/acme/widgets/pull/9" in sent[0][3]
    assert "https://github.com/acme/widgets/issues/42" in sent[0][3]
    assert "activationType', 'protocol'" in sent[0][3]
    assert logs == ["Windows notification sent."]


def test_ticket_pr_comment_uses_full_template_and_runtime_values():
    result = {
        **_result(0.95),
        "summary": "prevent duplicate widgets",
        "root_cause": "the create path did not check for an existing widget",
        "evidence": ["widgets.py:42"],
        "files_changed": ["widgets.py", "test_widgets.py"],
        "completion_requirements": [],
        "pr_notes": "Review the uniqueness check.",
    }
    comment = WorkflowRunner._build_ticket_pr_comment(
        result,
        {"verdict": "PASS", "findings": []},
        42,
        "main",
        "bug-fix/42-duplicates",
        {"widgets": "https://github.com/acme/widgets/pull/99"},
        {"repro_steps": ["Create the widget twice."], "pass_steps": ["Confirm the duplicate is rejected."]},
        754,
    )

    assert comment.startswith("# Summary")
    assert "## Linked Work" in comment
    assert "**Issue:** #42" in comment
    assert "https://github.com/acme/widgets/pull/99" in comment
    assert "**PR type:** `Bug Fix`" in comment
    assert "**Time to PR:** `12m 34s`" in comment
    assert "## Investigation" in comment
    assert "## Data and Security" in comment
    assert "## Final Checklist" in comment
    assert "Create the widget twice." in comment


def test_review_runs_in_disposable_checkout_and_cannot_change_original_source(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    source = repo / "app.py"
    source.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    source.write_text("implementation change\n", encoding="utf-8")

    review_root = tmp_path / "review-workspaces"
    review_root.mkdir()

    class ReviewSettings:
        workspace_root = review_root
        command_timeout_seconds = 10
        review_timeout_seconds = 10

    class ReviewStore:
        def get(self, job_id):
            return {"status": "running", "parameters": {}}

        def update(self, job_id, **changes):
            pass

        def append_log(self, job_id, message):
            pass

    attempted = {"blocked": False, "isolated_write": False, "cwd": None}

    def fake_reviewer(command, *, cwd, prompt, timeout, log, should_abort=None):
        attempted["cwd"] = cwd
        try:
            (cwd / "app.py").write_text("reviewer mutation\n", encoding="utf-8")
        except PermissionError:
            attempted["blocked"] = True
        else:
            attempted["isolated_write"] = True
        result_path = cwd / ".ticket-agent" / "review.json"
        result_path.write_text(
            json.dumps({"verdict": "PASS", "summary": "Looks good", "findings": []}),
            encoding="utf-8",
        )

    monkeypatch.setattr(workflow, "run_configured_command", fake_reviewer)
    runner = WorkflowRunner(ReviewSettings(), ReviewStore())
    review = runner._review(
        "job", repo, {"number": 42, "title": "Fix it"}, "main", "reviewer", lambda message: None
    )

    assert attempted["cwd"] != repo
    assert attempted["blocked"] or attempted["isolated_write"]
    assert source.read_text(encoding="utf-8") == "implementation change\n"
    assert review == {"verdict": "PASS", "summary": "Looks good", "findings": []}
    assert json.loads((repo / ".ticket-agent" / "review.json").read_text(encoding="utf-8")) == review
    assert list(review_root.iterdir()) == []


def test_changed_repositories_are_reviewed_in_parallel(monkeypatch, tmp_path):
    runner = WorkflowRunner(FakeSettings(), FakeStore())
    barrier = threading.Barrier(2)
    calls = []
    calls_lock = threading.Lock()

    def fake_review(
        job_id, repo_dir, issue, base_branch, command, log,
        implementation=None, manage_stage=True, delivery_started_at=None,
    ):
        with calls_lock:
            calls.append((repo_dir.name, implementation, manage_stage))
        barrier.wait(timeout=2)
        return {"verdict": "PASS", "summary": repo_dir.name, "findings": []}

    monkeypatch.setattr(runner, "_review", fake_review)
    monkeypatch.setattr(
        runner,
        "_coordinated_review_context",
        lambda repositories: {"crm-api": {"diff": "api change"}},
    )
    implementation = {"summary": "coordinated fix"}

    reviews = runner._review_changed_repositories(
        "job",
        {"crm-api": tmp_path / "crm-api", "crm-staff-desktop": tmp_path / "crm-staff-desktop"},
        {"number": 202, "title": "Matrix project"},
        implementation,
        "develop",
        "reviewer",
        lambda message: None,
    )

    assert list(reviews) == ["crm-api", "crm-staff-desktop"]
    assert all(call[1]["summary"] == "coordinated fix" and call[2] is False for call in calls)
    contexts = {name: details["coordinated_repository_changes"] for name, details, _ in calls}
    assert contexts["crm-api"] == {}
    assert "crm-api" in contexts["crm-staff-desktop"]


def test_full_delivery_target_does_not_shorten_phase_safety_timeout(monkeypatch):
    class Settings(FakeSettings):
        full_delivery_target_seconds = 180
        agent_pass_timeout_seconds = 110

    runner = WorkflowRunner(Settings(), FakeStore())
    monkeypatch.setattr(workflow.time, "time", lambda: 170.0)

    assert runner._delivery_timeout(110, 0.0, floor_seconds=5) == 110


def test_full_delivery_reserves_do_not_turn_target_into_a_failure_gate(monkeypatch):
    class Settings(FakeSettings):
        full_delivery_target_seconds = 180

    runner = WorkflowRunner(Settings(), FakeStore())
    monkeypatch.setattr(workflow.time, "time", lambda: 20.0)

    assert runner._delivery_timeout(110, 0.0, floor_seconds=20, reserve_seconds=75) == 110


def test_full_delivery_reserves_clamp_legacy_long_review_timeout():
    class Settings(FakeSettings):
        full_delivery_target_seconds = 180
        review_timeout_seconds = 1800
        full_delivery_publish_reserve_seconds = 30

    runner = WorkflowRunner(Settings(), FakeStore())

    assert runner._full_delivery_reserves() == (0, 30)


def test_zero_model_timeouts_disable_review_deadlines():
    class Settings(FakeSettings):
        review_timeout_seconds = 0

    runner = WorkflowRunner(Settings(), FakeStore())

    assert runner._review_timeout("codex exec --json -") is None


def test_full_delivery_continues_after_soft_target_is_exhausted(monkeypatch):
    class Settings(FakeSettings):
        full_delivery_target_seconds = 180
        agent_pass_timeout_seconds = 110

    runner = WorkflowRunner(Settings(), FakeStore())
    monkeypatch.setattr(workflow.time, "time", lambda: 176.0)

    assert runner._delivery_timeout(110, 0.0, floor_seconds=5) == 110


def test_coordinated_review_context_includes_sibling_diff_and_untracked_files(tmp_path):
    repositories = {}
    for name in ("crm-api", "crm-staff-desktop"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        source = repo / "source.txt"
        source.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "source.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
        source.write_text(f"after {name}\n", encoding="utf-8")
        (repo / "new-schema.txt").write_text(f"schema for {name}\n", encoding="utf-8")
        repositories[name] = repo

    context = WorkflowRunner._coordinated_review_context(repositories)

    assert list(context) == ["crm-api", "crm-staff-desktop"]
    assert "after crm-api" in context["crm-api"]["diff"]
    assert "untracked file: new-schema.txt" in context["crm-api"]["diff"]
    assert "schema for crm-staff-desktop" in context["crm-staff-desktop"]["diff"]


def test_local_workspace_jobs_are_serialized(tmp_path):
    class LocalSettings(FakeSettings):
        local_repo_path = tmp_path

    runner = WorkflowRunner(LocalSettings(), FakeStore())
    state_lock = threading.Lock()
    first_entered = threading.Event()
    active = 0
    peak_active = 0

    def fake_run(job_id):
        nonlocal active, peak_active
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
            first_entered.set()
        time.sleep(0.05)
        with state_lock:
            active -= 1

    runner.run = fake_run
    first = threading.Thread(target=runner._run_with_slot, args=("one",))
    second = threading.Thread(target=runner._run_with_slot, args=("two",))
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert peak_active == 1
