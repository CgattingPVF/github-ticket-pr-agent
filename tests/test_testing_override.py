import app as app_module


class OverrideStore:
    def __init__(self):
        self.job = {
            "id": "qa123",
            "status": "completed",
            "issue_url": "https://github.com/acme/widgets/issues/7",
            "parameters": {"workflow_profile": "testing_only"},
            "result": {
                "overall": "failed",
                "tests_run": [
                    {"command": "focused test", "result": "failed", "notes": "negative fixture id"},
                    {"command": "build", "result": "passed", "notes": "clean"},
                ],
            },
        }

    def get(self, job_id):
        return self.job if job_id == "qa123" else None

    def update(self, job_id, **fields):
        self.job["result"] = fields["result_json"]


def test_operator_can_override_failed_qa_with_audited_reason(monkeypatch):
    fake_store = OverrideStore()
    comments = []
    monkeypatch.setattr(app_module, "store", fake_store)
    monkeypatch.setattr(app_module, "post_issue_comment", lambda url, body: comments.append((url, body)))
    monkeypatch.setattr(
        app_module,
        "GitHubOps",
        lambda *args, **kwargs: type("ProjectOps", (), {"mark_issue_qa_done": lambda self, ref: {"Status": 1, "Test State": 1}})(),
    )
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().post(
        "/testing/jobs/qa123/override",
        json={"index": 0, "status": "passed", "reason": "Negative IDs cannot occur in production."},
    )

    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["overall"] == "passed"
    assert result["tests_run"][0]["result"] == "passed"
    assert result["tests_run"][0]["automated_result"] == "failed"
    assert result["tests_run"][0]["operator_override"]["reason"] == "Negative IDs cannot occur in production."
    assert result["project_status"]["status"] == "Done"
    assert result["project_status"]["test_state"] == "Pass"
    assert "original machine result remains recorded" in comments[0][1]


def test_operator_override_requires_a_reason(monkeypatch):
    monkeypatch.setattr(app_module, "store", OverrideStore())
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().post(
        "/testing/jobs/qa123/override",
        json={"index": 0, "status": "passed", "reason": ""},
    )

    assert response.status_code == 400
    assert "Explain why" in response.get_json()["error"]


def test_skipped_checks_do_not_prevent_a_successful_override(monkeypatch):
    fake_store = OverrideStore()
    fake_store.job["result"]["tests_run"][1]["result"] = "skipped"
    monkeypatch.setattr(app_module, "store", fake_store)
    monkeypatch.setattr(app_module, "post_issue_comment", lambda *args: None)
    monkeypatch.setattr(
        app_module,
        "GitHubOps",
        lambda *args, **kwargs: type("ProjectOps", (), {"mark_issue_qa_done": lambda self, ref: {"Status": 1, "Test State": 1}})(),
    )
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().post(
        "/testing/jobs/qa123/override",
        json={"index": 0, "status": "passed", "reason": "Impossible production fixture."},
    )

    assert response.get_json()["result"]["overall"] == "passed"
