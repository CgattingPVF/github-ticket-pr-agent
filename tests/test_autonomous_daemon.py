import app as app_module


def test_autonomous_daemon_dispatches_fix_workflow_not_testing(monkeypatch):
    issue_url = "https://github.com/acme/widgets/issues/7"
    created = []
    started = []

    class FakeStore:
        def list_tickets(self, state="OPEN"):
            return [{"url": issue_url, "priority": "P1"}]

        def create(self, parameters):
            created.append(parameters)
            return "job123"

    class FakeRunner:
        def start(self, job_id):
            started.append(job_id)

        def start_testing(self, job_id):
            raise AssertionError("Autonomous Daemon must not dispatch Testing Lab")

    monkeypatch.setattr(app_module, "store", FakeStore())
    monkeypatch.setattr(app_module, "runner", FakeRunner())
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().post(
        "/jobs",
        data={
            "issue_url": issue_url,
            "base_branch": "develop",
            "branch_prefix": "bug-fix",
            "workflow_profile": "full_pr",
        },
    )

    assert response.status_code == 302
    assert created[0]["workflow_profile"] == "full_pr"
    assert created[0]["contract_reward"] == 1800
    assert started == ["job123"]
