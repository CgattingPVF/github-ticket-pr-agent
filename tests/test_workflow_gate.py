import json
import subprocess
import threading
import time
from pathlib import Path

import workflow
from workflow import WorkflowRunner


class FakeStore:
    def get(self, job_id):
        return {"status": "running", "parameters": {}}

    def append_log(self, job_id, message):
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
