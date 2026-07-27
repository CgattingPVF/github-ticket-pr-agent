import app as app_module
from store import JobStore


def test_job_api_returns_only_new_telemetry_after_cursor(monkeypatch):
    class FakeStore:
        def get(self, job_id):
            return {
                "id": job_id,
                "status": "running",
                "stage": "Reviewing the change",
                "logs": "first\nsecond\nthird\n",
                "parameters": {},
            }

    monkeypatch.setattr(app_module, "store", FakeStore())
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get("/api/jobs/job123?log_offset=6")

    assert response.status_code == 200
    payload = response.get_json()
    assert "logs" not in payload
    assert payload["logs_delta"] == "second\nthird\n"
    assert payload["log_offset"] == len("first\nsecond\nthird\n")


def test_job_api_resets_stale_telemetry_cursor(monkeypatch):
    class FakeStore:
        def get(self, job_id):
            return {"id": job_id, "logs": "fresh\n", "parameters": {}}

    monkeypatch.setattr(app_module, "store", FakeStore())
    app_module.app.config.update(TESTING=True)

    payload = app_module.app.test_client().get(
        "/api/jobs/job123?log_offset=999"
    ).get_json()

    assert payload["logs_reset"] is True
    assert payload["logs_delta"] == "fresh\n"
    assert payload["log_offset"] == len("fresh\n")


def test_job_store_uses_append_only_log_chunks_and_cursor_deltas(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    job_id = store.create({
        "issue_url": "https://github.com/acme/widgets/issues/7",
        "base_branch": "develop",
    })

    store.append_log(job_id, "first")
    first = store.get_updates(job_id, 0)
    store.append_log(job_id, "second")
    second = store.get_updates(job_id, first["log_cursor"])

    assert first["logs_delta"] == "first\n"
    assert second["logs_delta"] == "second\n"
    assert second["log_cursor"] > first["log_cursor"]
    assert "logs" not in store.get(job_id)
    assert store.get_with_logs(job_id)["logs"] == "first\nsecond\n"


def test_job_list_does_not_load_large_raw_reports(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    job_id = store.create({
        "issue_url": "https://github.com/acme/widgets/issues/8",
        "base_branch": "develop",
    })
    store.append_log(job_id, "x" * 100_000)

    assert store.list(limit=1)[0]["logs"] == ""


def test_single_huge_telemetry_event_is_compacted(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    job_id = store.create({
        "issue_url": "https://github.com/acme/widgets/issues/9",
        "base_branch": "develop",
    })
    store.append_log(job_id, "A" * 50_000)

    logs = store.get_with_logs(job_id)["logs"]
    assert len(logs) < 17_000
    assert "telemetry compacted" in logs
