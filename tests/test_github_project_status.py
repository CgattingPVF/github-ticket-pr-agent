import json
from types import SimpleNamespace

import github_ops
from core import IssueRef
from github_ops import GitHubOps


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
    assert len(calls) == 3
    assert 'singleSelectOptionId: "DONE"' in calls[1][-1]
    assert 'singleSelectOptionId: "PASS"' in calls[2][-1]
