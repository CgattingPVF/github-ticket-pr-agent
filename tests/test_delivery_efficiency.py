import dataclasses
import time

from config import Settings
from store import JobStore
from workflow import WorkflowRunner


def _runner(tmp_path):
    settings = Settings()
    store = JobStore(tmp_path / "jobs.sqlite3")
    return WorkflowRunner(settings, store), store


def test_stage_timing_report_computes_avg_median_slowest(tmp_path):
    runner, store = _runner(tmp_path)
    job_id = store.create({"issue_url": "https://github.com/o/r/issues/1", "base_branch": "main"})
    store.record_stage_timing(job_id, "Running validation", 100)
    store.record_stage_timing(job_id, "Running validation", 300)
    store.record_stage_timing(job_id, "Reading ticket", 50)

    report = store.get_stage_timing_report()
    by_stage = {row["stage"]: row for row in report}

    assert by_stage["Running validation"]["count"] == 2
    assert by_stage["Running validation"]["avg_ms"] == 200
    assert by_stage["Running validation"]["median_ms"] == 200
    assert by_stage["Running validation"]["slowest_ms"] == 300
    # Sorted slowest-average-first.
    assert report[0]["stage"] == "Running validation"


def test_timed_stage_flags_long_running_stage(tmp_path, monkeypatch):
    runner, store = _runner(tmp_path)
    runner.settings = dataclasses.replace(runner.settings, stage_stall_threshold_ms=0)
    job_id = store.create({"issue_url": "https://github.com/o/r/issues/1", "base_branch": "main"})

    with runner._timed_stage(job_id, "Slow stage"):
        time.sleep(0.01)

    logs = store.get_with_logs(job_id)["logs"]
    assert "possible stall" in logs


def test_estimate_ticket_risk_low_for_short_ticket():
    issue = {"title": "Fix typo", "body": "Change 'teh' to 'the' on the login page.", "labels": []}
    assert WorkflowRunner._estimate_ticket_risk(issue) == "low"


def test_estimate_ticket_risk_low_for_labeled_ticket():
    issue = {"title": "x" * 200, "body": "y" * 2000, "labels": [{"name": "size:small"}]}
    assert WorkflowRunner._estimate_ticket_risk(issue) == "low"


def test_estimate_ticket_risk_normal_for_long_ticket():
    issue = {
        "title": "Investigate and fix the intermittent checkout failure",
        "body": "\n".join(f"- step {i}" for i in range(10)) + ("z" * 1000),
        "labels": [],
    }
    assert WorkflowRunner._estimate_ticket_risk(issue) == "normal"


def test_validation_and_review_run_concurrently(tmp_path, monkeypatch):
    runner, store = _runner(tmp_path)
    job_id = store.create({"issue_url": "https://github.com/o/r/issues/1", "base_branch": "main"})

    def slow_integrity(*args, **kwargs):
        time.sleep(0.2)
        return []

    def slow_review(*args, **kwargs):
        time.sleep(0.2)
        return {"repo": {"findings": []}}

    monkeypatch.setattr(runner, "_run_integrity_checks", slow_integrity)
    monkeypatch.setattr(runner, "_review_changed_repositories", slow_review)

    started = time.time()
    integrity_checks, review, reviews = runner._run_validation_and_review(
        job_id, {"repo": tmp_path}, [], object(), lambda msg: None,
        {}, {}, "main", "echo", None,
    )
    elapsed = time.time() - started

    assert elapsed < 0.35  # concurrent, not 0.4s serial
    assert review["verdict"] == "PASS"
    assert reviews == {"repo": {"findings": []}}


def test_review_fingerprint_detects_identical_blocking_findings():
    review_a = {"findings": [{"severity": "HIGH", "summary": "Null check missing"}]}
    review_b = {"findings": [{"severity": "HIGH", "summary": "Null check missing"}]}
    review_c = {"findings": [{"severity": "HIGH", "summary": "Different failure"}]}

    fp_a = WorkflowRunner._review_fingerprint(review_a)
    fp_b = WorkflowRunner._review_fingerprint(review_b)
    fp_c = WorkflowRunner._review_fingerprint(review_c)

    assert fp_a == fp_b
    assert fp_a != fp_c
