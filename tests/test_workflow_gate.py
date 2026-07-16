import json
import subprocess
import threading
import time
from pathlib import Path

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


def _result(confidence):
    return {
        "safe_to_pr": True, "confidence": confidence, "summary": "s",
        "root_cause": "r", "tests_run": [{"command": "t", "result": "passed"}],
        "unresolved_risks": [], "commit_message": "m", "pr_title": "p",
    }


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
    assert "rejected your last" in calls[1]


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


def test_pr_body_static_builder_handles_reported_tests():
    body = WorkflowRunner._build_pr_body(
        _result(0.95),
        "## Automated review\n\nPASS",
        "Fixes",
        "acme/widgets#42",
        "main",
        "main",
    )

    assert "`t`: **passed**" in body
    assert "Fixes acme/widgets#42" in body


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
    )

    assert comment.startswith("# Summary")
    assert "## Linked Work" in comment
    assert "**Issue:** #42" in comment
    assert "https://github.com/acme/widgets/pull/99" in comment
    assert "**PR type:** `Bug Fix`" in comment
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

    def fake_reviewer(command, *, cwd, prompt, timeout, log):
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
