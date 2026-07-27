from types import SimpleNamespace

import app as app_module
from store import JobStore


def _job(
    job_id,
    status,
    updated_at,
    issue_url,
    *,
    profile="full_pr",
    reward=900,
):
    return {
        "id": job_id,
        "status": status,
        "created_at": updated_at,
        "updated_at": updated_at,
        "issue_url": issue_url,
        "parameters": {
            "workflow_profile": profile,
            "contract_reward": reward,
        },
        "result": {},
    }


def test_progression_uses_historic_pr_baseline_then_unique_completed_contracts():
    jobs = [
        _job("old", "completed", "2026-01-01T12:00:00+00:00", "https://github.com/acme/app/issues/1"),
        _job("contract", "completed", "2026-01-03T12:00:00+00:00", "https://github.com/acme/app/issues/2", reward=1800),
        _job("duplicate", "completed", "2026-01-04T12:00:00+00:00", "https://github.com/acme/app/issues/2", reward=1800),
        _job("scanner", "completed", "2026-01-05T12:00:00+00:00", "https://github.com/acme/app/issues/3", profile="testing_only"),
        _job("failed", "failed", "2026-01-06T12:00:00+00:00", "https://github.com/acme/app/issues/4"),
    ]

    stats = app_module.player_stats(
        jobs,
        historic_prs=5,
        baseline_at="2026-01-02T12:00:00+00:00",
    )

    assert stats["historic_prs"] == 5
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["xp"] == (5 + 1) * app_module.XP_COMPLETED
    assert stats["credits_banked"] == (
        5 * app_module.EURODOLLARS_PER_HISTORIC_PR + 1800
    )
    assert stats["credits_rate"] == 0


def test_eurodollars_do_not_change_with_elapsed_time():
    jobs = [
        _job("contract", "completed", "2020-01-01T12:00:00+00:00", "https://github.com/acme/app/issues/2", reward=1200),
    ]

    first = app_module.player_stats(jobs, historic_prs=2)
    second = app_module.player_stats(jobs, historic_prs=2)

    assert first["credits_banked"] == second["credits_banked"] == 3000


def test_player_progression_baseline_is_initialized_only_once(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.upsert_player("v", "V", "")

    first = store.initialize_player_progression("v", 12)
    second = store.initialize_player_progression("v", 99)

    assert first["historic_prs"] == 12
    assert second["historic_prs"] == 12
    assert second["progression_baseline_at"] == first["progression_baseline_at"]


def test_failed_contracts_do_not_raise_leaderboard_street_cred(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.upsert_player("v", "V", "")
    store.initialize_player_progression("v", 3)
    job_id = store.create({
        "issue_url": "https://github.com/acme/app/issues/7",
        "base_branch": "develop",
        "github_login": "v",
    })
    store.update(job_id, status="failed")

    player = store.leaderboard()[0]

    assert player["failed"] == 1
    assert player["completed"] == 0
    assert player["xp"] == 3 * app_module.XP_COMPLETED


def test_historic_pr_count_comes_from_github_search(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"total_count": 27}', stderr="")

    monkeypatch.setattr(app_module, "find_gh_executable", lambda: "gh")
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    with app_module.app.test_request_context("/"):
        app_module.session["github_token"] = "secret"
        assert app_module.fetch_historic_merged_pr_count("v") == 27

    args, kwargs = calls[0]
    assert args[0:5] == ["gh", "api", "-X", "GET", "search/issues"]
    assert "q=is:pr is:merged author:v" in args
    assert kwargs["env"]["GH_TOKEN"] == "secret"


def test_hud_numbers_use_apostrophe_grouping():
    assert app_module.group_digits(143100) == "143'100"
    assert app_module.group_digits(2500) == "2'500"
