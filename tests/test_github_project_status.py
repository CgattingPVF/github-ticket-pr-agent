import json
from types import SimpleNamespace

import github_ops
from core import IssueRef
from github_ops import GitHubOps


def test_full_delivery_issue_context_uses_one_bounded_graphql_call(monkeypatch):
    calls = []
    payload = {
        "data": {"repository": {
            "defaultBranchRef": {"name": "main"},
            "issue": {
                "number": 7, "title": "Fix widgets", "body": "details",
                "state": "OPEN", "url": "https://github.com/acme/widgets/issues/7",
                "updatedAt": "2026-07-19T00:00:00Z",
                "labels": {"nodes": [{"name": "bug"}]},
                "comments": {"nodes": [{
                    "author": {"login": "clayton"}, "createdAt": "now", "body": "latest",
                }]},
                "closedByPullRequestsReferences": {"nodes": []},
            },
        }},
    }

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

    monkeypatch.setattr(github_ops, "run_command", fake_run)
    issue = GitHubOps(10, lambda message: None).get_issue_with_compact_context(
        IssueRef(owner="acme", repo="widgets", number=7)
    )

    assert len(calls) == 1
    assert issue["repository_default_branch"] == "main"
    assert issue["mergequest_github_context"]["latest_comments"][0]["body"] == "latest"


def test_compact_issue_context_fetches_comments_and_linked_pr_checks(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "comments?per_page=100" in command[2]:
            return SimpleNamespace(stdout=json.dumps([
                {"user": {"login": "one"}, "created_at": "old", "body": "old"},
                {"user": {"login": "two"}, "created_at": "new", "body": "new"},
            ]), returncode=0)
        return SimpleNamespace(stdout=json.dumps({
            "data": {
                "repository": {
                    "issue": {
                        "closedByPullRequestsReferences": {
                            "nodes": [{
                                "number": 12,
                                "title": "Fix widgets",
                                "url": "https://github.com/acme/widgets/pull/12",
                                "state": "OPEN",
                                "changedFiles": 4,
                                "baseRefName": "develop",
                                "headRefName": "fix/widgets",
                                "repository": {"nameWithOwner": "acme/widgets"},
                                "commits": {"nodes": [{
                                    "commit": {
                                        "statusCheckRollup": {
                                            "contexts": {"nodes": [
                                                {"name": "pytest", "conclusion": "SUCCESS"},
                                                {"context": "lint", "state": "PENDING"},
                                            ]}
                                        }
                                    }
                                }]},
                            }]
                        }
                    }
                }
            }
        }), returncode=0)

    monkeypatch.setattr(github_ops, "run_command", fake_run)

    context = GitHubOps(10, lambda message: None).get_compact_issue_context(
        IssueRef(owner="acme", repo="widgets", number=7)
    )

    assert context["latest_comments"][-1]["body"] == "new"
    assert context["linked_pull_requests"][0]["changed_files"] == 4
    assert context["linked_pull_requests"][0]["checks"] == [
        {"name": "pytest", "status": "SUCCESS"},
        {"name": "lint", "status": "PENDING"},
    ]
    assert len(calls) == 2


def test_successful_qa_sets_project_status_done_and_test_state_pass(monkeypatch):
    calls = []
    metadata = {
        "data": {
            "repository": {
                "issue": {
                    "projectItems": {
                        "nodes": [{
                            "id": "ITEM_1",
                            "project": {
                                "id": "PROJECT_1",
                                "fields": {
                                    "nodes": [
                                        {"id": "STATUS_FIELD", "name": "Status", "options": [{"id": "DONE", "name": "Done"}]},
                                        {"id": "TEST_FIELD", "name": "Test State", "options": [{"id": "PASS", "name": "Pass"}]},
                                    ]
                                },
                            },
                        }]
                    }
                }
            }
        }
    }

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(metadata) if len(calls) == 1 else '{}')

    monkeypatch.setattr(github_ops, "run_command", fake_run)
    counts = GitHubOps(10, lambda message: None).mark_issue_qa_done(
        IssueRef(owner="acme", repo="widgets", number=7)
    )

    assert counts == {"Status": 1, "Test State": 1}
    assert len(calls) == 2
    assert 'singleSelectOptionId: "DONE"' in calls[1][-1]
    assert 'singleSelectOptionId: "PASS"' in calls[1][-1]


def test_successful_qa_with_open_pr_sets_pr_ready_and_clears_test_state(monkeypatch):
    calls = []
    metadata = {
        "data": {
            "repository": {
                "issue": {
                    "projectItems": {
                        "nodes": [{
                            "id": "ITEM_1",
                            "project": {
                                "id": "PROJECT_1",
                                "fields": {
                                    "nodes": [
                                        {
                                            "id": "STATUS_FIELD",
                                            "name": "Status",
                                            "options": [{"id": "PR_READY", "name": "PR Ready"}],
                                        },
                                        {
                                            "id": "TEST_FIELD",
                                            "name": "Test State",
                                            "options": [{"id": "PASS", "name": "Pass"}],
                                        },
                                    ]
                                },
                            },
                        }]
                    }
                }
            }
        }
    }

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(metadata) if len(calls) == 1 else '{}')

    monkeypatch.setattr(github_ops, "run_command", fake_run)
    counts = GitHubOps(10, lambda message: None).mark_issue_pr_ready(
        IssueRef(owner="acme", repo="widgets", number=7)
    )

    assert counts == {"Status": 1, "Test State": 1}
    assert len(calls) == 2
    assert 'singleSelectOptionId: "PR_READY"' in calls[1][-1]
    assert "clearProjectV2ItemFieldValue" in calls[1][-1]
    assert 'projectId: "PROJECT_1"' in calls[1][-1]
    assert 'itemId: "ITEM_1"' in calls[1][-1]
    assert 'fieldId: "TEST_FIELD"' in calls[1][-1]
    assert 'singleSelectOptionId: "PASS"' not in calls[1][-1]


def test_successful_qa_transition_uses_live_open_pr_state(monkeypatch):
    ops = GitHubOps(10, lambda message: None)
    ref = IssueRef(owner="acme", repo="widgets", number=7)
    calls = []
    monkeypatch.setattr(ops, "has_linked_open_pr", lambda issue, repositories: True)
    monkeypatch.setattr(
        ops,
        "mark_issue_pr_ready",
        lambda issue: calls.append("pr_ready") or {"Status": 1, "Test State": 1},
    )
    monkeypatch.setattr(
        ops,
        "mark_issue_qa_done",
        lambda issue: calls.append("done") or {"Status": 1, "Test State": 1},
    )

    result = ops.sync_successful_qa_project_fields(ref, ["widgets", "widgets-api"])

    assert calls == ["pr_ready"]
    assert result == {
        "updated": True,
        "count": 1,
        "test_state_count": 1,
        "status": "PR Ready",
        "test_state": None,
        "has_open_pr": True,
    }


def test_open_pr_state_is_queried_live_across_linked_repository(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        prs = [] if args[4] == "acme/widgets" else [{
            "number": 14,
            "url": "https://github.com/acme/widgets-api/pull/14",
            "body": "Fixes acme/widgets#7",
            "title": "Fix widget",
        }]
        return SimpleNamespace(stdout=json.dumps(prs))

    monkeypatch.setattr(github_ops, "run_command", fake_run)
    has_open_pr = GitHubOps(10, lambda message: None).has_linked_open_pr(
        IssueRef(owner="acme", repo="widgets", number=7),
        ["widgets", "widgets-api"],
    )

    assert has_open_pr is True
    pr_list_calls = [call for call in calls if call[:3] == ["gh", "pr", "list"]]
    assert [call[4] for call in pr_list_calls] == ["acme/widgets", "acme/widgets-api"]
    assert all(call[5:7] == ["--state", "open"] for call in pr_list_calls)


def test_open_pr_state_uses_github_development_panel_attachments(monkeypatch):
    def fake_run(args, **kwargs):
        if args[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(stdout="[]", returncode=0)
        payload = {"data": {"repository": {"issue": {
            "closedByPullRequestsReferences": {"nodes": [{
                "number": 207,
                "url": "https://github.com/acme/widgets-api/pull/207",
                "state": "OPEN",
                "headRefName": "feature/priority-filters",
                "baseRefName": "develop",
                "body": "No issue reference in this body",
                "title": "Add API support",
                "repository": {"nameWithOwner": "acme/widgets-api"},
            }]},
        }}}}
        return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

    monkeypatch.setattr(github_ops, "run_command", fake_run)

    pr = GitHubOps(10, lambda message: None).linked_open_pr(
        IssueRef(owner="acme", repo="widgets-api", number=7),
        IssueRef(owner="acme", repo="widgets", number=7),
    )

    assert pr["number"] == 207
    assert pr["headRefName"] == "feature/priority-filters"


def test_open_pr_state_accepts_multiple_linked_prs_for_boolean_transition(monkeypatch):
    prs = [
        {"number": 14, "body": "Fixes acme/widgets#7"},
        {"number": 15, "body": "Relates to https://github.com/acme/widgets/issues/7"},
    ]
    monkeypatch.setattr(
        github_ops,
        "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(prs)),
    )

    assert GitHubOps(10, lambda message: None).has_linked_open_pr(
        IssueRef(owner="acme", repo="widgets", number=7),
    ) is True


def test_open_pr_state_does_not_match_longer_issue_number(monkeypatch):
    prs = [
        {"number": 14, "body": "Fixes acme/widgets#70"},
        {"number": 15, "body": "Fixes https://github.com/acme/widgets/issues/70"},
    ]
    monkeypatch.setattr(
        github_ops,
        "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(prs)),
    )

    assert GitHubOps(10, lambda message: None).has_linked_open_pr(
        IssueRef(owner="acme", repo="widgets", number=7),
    ) is False


def test_no_open_pr_keeps_merged_or_closed_ticket_on_legacy_transition(monkeypatch):
    monkeypatch.setattr(
        github_ops,
        "run_command",
        lambda *args, **kwargs: SimpleNamespace(stdout="[]"),
    )

    assert GitHubOps(10, lambda message: None).has_linked_open_pr(
        IssueRef(owner="acme", repo="widgets", number=7),
    ) is False


def test_successful_qa_transition_keeps_done_pass_without_open_pr(monkeypatch):
    ops = GitHubOps(10, lambda message: None)
    ref = IssueRef(owner="acme", repo="widgets", number=7)
    calls = []
    monkeypatch.setattr(ops, "has_linked_open_pr", lambda issue, repositories: False)
    monkeypatch.setattr(
        ops,
        "mark_issue_pr_ready",
        lambda issue: calls.append("pr_ready") or {"Status": 1, "Test State": 1},
    )
    monkeypatch.setattr(
        ops,
        "mark_issue_qa_done",
        lambda issue: calls.append("done") or {"Status": 1, "Test State": 1},
    )

    result = ops.sync_successful_qa_project_fields(ref, ["widgets"])

    assert calls == ["done"]
    assert result["status"] == "Done"
    assert result["test_state"] == "Pass"
    assert result["has_open_pr"] is False
