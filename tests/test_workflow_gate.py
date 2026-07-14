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
