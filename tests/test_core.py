import pytest
from pathlib import Path

from ticket_sync import import_workbook
from workflow import WorkflowRunner

from core import compress_agent_prompt, generate_test_plan, make_branch_name, parse_issue_url, parse_validation_commands, slugify, validate_ref_name
from prompts import all_in_one_prompt, automated_qa_prompt, compact_issue_payload, confidence_gate_prompt, investigation_prompt, review_prompt


def test_parse_issue_url():
    ref = parse_issue_url("https://github.com/acme/widgets/issues/123")
    assert ref.owner == "acme"
    assert ref.repo == "widgets"
    assert ref.number == 123


def test_compress_agent_prompt_preserves_edges_and_marks_omitted_context():
    prompt = "TASK\n" + ("middle context\n" * 5000) + "LATEST FAILURE\nconfidence=0.42"
    compressed = compress_agent_prompt(prompt, max_chars=4000)
    assert len(compressed) <= 4000
    assert compressed.startswith("TASK")
    assert compressed.endswith("LATEST FAILURE\nconfidence=0.42")
    assert "prompt compression" in compressed


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widgets/issues/1",
        "https://example.com/acme/widgets/issues/1",
        "https://github.com/acme/widgets",
    ],
)
def test_parse_issue_url_rejects_invalid_urls(url):
    with pytest.raises(ValueError):
        parse_issue_url(url)


def test_branch_name_is_safe_and_readable():
    assert make_branch_name("feature", 42, "Fix user's broken / API") == "feature/42-fix-user-s-broken-api"


def test_validate_ref_rejects_traversal():
    with pytest.raises(ValueError):
        validate_ref_name("feature/../main")


def test_validation_commands_are_not_shell_commands():
    commands = parse_validation_commands("python -m pytest\nbun run lint\n")
    assert commands == [["python", "-m", "pytest"], ["bun", "run", "lint"]]


def test_slugify_fallback():
    assert slugify("!!!") == "ticket-fix"


def test_slugify_converts_text_to_a_lowercase_slug():
    assert slugify("Fix Login Bug") == "fix-login-bug"


def test_validate_ref_rejects_option_like_names():
    with pytest.raises(ValueError):
        validate_ref_name("--upload-pack=evil")


def test_all_in_one_prompt_requires_confirmation_for_each_stage():
    prompt = all_in_one_prompt(
        {"number": 7, "title": "Fix login", "body": "It fails", "labels": []},
        "develop",
        "bug-fix/7-fix-login",
    )
    assert "INTERACTIVE STAGE CONTROL (MANDATORY)" in prompt
    assert 'Proceed with stage <number> (<name>)? (yes/no)' in prompt
    assert "Never combine confirmations" in prompt
    assert "7. Ask permission before creating the pull request" in prompt


def test_automated_qa_prompt_excludes_manual_ui_verification():
    prompt = automated_qa_prompt({"number": 7, "title": "Animation", "body": "", "labels": []}, ".qa.json")
    assert "Manual UI testing is outside this automated report" in prompt
    assert "Do not add UI interaction" in prompt


def test_autonomous_daemon_prompt_fixes_without_running_tests():
    prompt = investigation_prompt(
        {"number": 7, "title": "Fix login", "body": "It fails", "labels": []},
        "develop",
        "bug-fix/7-fix-login",
    )
    assert "DO NOT run tests, builds, type checks, lint, code/Prisma generation" in prompt
    assert "surrounding Integrity Scan handles validation" in prompt
    assert "Record tests_run as []" in prompt
    assert "Run one focused pass/fail test" not in prompt


def test_investigation_prompt_uses_compact_github_context():
    long_body = "body " * 1000
    long_comment = "comment " * 500
    prompt = investigation_prompt(
        {
            "number": 7,
            "title": "Fix login",
            "body": long_body,
            "labels": [],
            "mergequest_github_context": {
                "latest_comments": [
                    {"author": "clayton", "created_at": "2026-07-19T12:00:00Z", "body": long_comment}
                ],
                "linked_pull_requests": [
                    {
                        "repository": "acme/widgets",
                        "number": 8,
                        "title": "Existing fix",
                        "state": "OPEN",
                        "url": "https://github.com/acme/widgets/pull/8",
                        "base": "develop",
                        "head": "fix/login",
                        "changed_files": 3,
                        "checks": [{"name": "pytest", "status": "SUCCESS"}],
                    }
                ],
            },
        },
        "develop",
        "bug-fix/7-fix-login",
    )

    assert "body_excerpt" in prompt
    assert "latest_comments" in prompt
    assert "linked_pull_requests" in prompt
    assert "Existing fix" in prompt
    assert len(prompt) < 9000
    assert "body " * 600 not in prompt
    assert "comment " * 300 not in prompt
    compressed = compress_agent_prompt(prompt, max_chars=4000)
    assert "FULL DELIVERY SPEED CONTRACT" in compressed
    assert "DO NOT run tests, builds, type checks, lint, code/Prisma generation" in compressed
    assert "Never scan parent directories or use broad `find ..` searches" in compressed


def test_confidence_retry_prompt_uses_configured_threshold():
    prompt = confidence_gate_prompt({"number": 7}, "too low", minimum_confidence=0.80)

    assert "confidence >= 0.80" in prompt
    assert "confidence >= 0.90" not in prompt


def test_compact_issue_payload_limits_context_lists():
    payload = compact_issue_payload({
        "number": 7,
        "title": "Fix",
        "body": "short",
        "labels": [],
        "mergequest_github_context": {
            "latest_comments": [{"body": str(index)} for index in range(8)],
            "linked_pull_requests": [{"number": index} for index in range(8)],
            "context_warnings": ["a", "b", "c", "d"],
        },
    })

    assert len(payload["latest_comments"]) == 5
    assert len(payload["linked_pull_requests"]) == 5
    assert payload["context_warnings"] == ["a", "b", "c"]


def test_qa_fix_prompt_still_runs_focused_validation():
    prompt = investigation_prompt(
        {"number": 7, "title": "Fix login", "body": "It fails", "labels": []},
        "develop",
        "bug-fix/7-fix-login",
        run_validation=True,
    )
    assert "Run only one focused behavior test" in prompt
    assert "Record tests_run as an empty list" not in prompt


def test_review_prompt_receives_ticket_and_implementation_context():
    prompt = review_prompt(
        {"number": 7, "title": "Fix login", "body": "Expected: login succeeds"},
        "develop",
        {
            "summary": "Handle the callback",
            "root_cause": "Callback state was discarded",
            "files_changed": ["auth.py"],
            "tests_run": [{"command": "git diff --check", "result": "passed"}],
        },
    )

    assert "Expected: login succeeds" in prompt
    assert "Callback state was discarded" in prompt
    assert '"files_changed":[' in prompt
    assert "Start with `git status --short`, `git diff --stat`, and `git diff`" in prompt
    assert "coordinated pull requests in multiple repositories" in prompt
    assert "Do not report a required sibling change as missing" in prompt


def test_supplied_workbook_imports_github_tickets():
    tickets = import_workbook(Path('bug_tracker_items (1).xlsx'))
    assert len(tickets) > 100
    assert tickets[0]['repository'] == 'pvfscaffolding/crm-staff-desktop'
    assert tickets[0]['number'] == 1065
    assert tickets[0]['url'].endswith('/issues/1065')


def test_failure_comment_explains_required_next_steps():
    comment = WorkflowRunner._failure_comment(
        "Running validation",
        "The coding agent reported a blocker",
        {
            "root_cause": "The ticket requires persisted audit history, but no audit table exists.",
            "completion_requirements": ["Add the audit-history schema and migration."],
            "tests_run": [{"command": "pytest", "result": "failed", "notes": "audit table is missing"}],
        },
        {},
    )

    assert "Running validation" in comment
    assert "no audit table exists" in comment
    assert "Add the audit-history schema and migration" in comment
    assert "Fix `pytest`: audit table is missing" in comment
    assert "Required to complete this ticket" in comment


def test_qa_prompt_distinguishes_unrelated_baseline_type_errors():
    prompt = automated_qa_prompt({"number": 996, "title": "Staff behavior"}, ".ticket-agent/qa.json")
    assert "Repository-wide checks need careful attribution" in prompt
    assert "unrelated pre-existing file" in prompt
    assert "Never claim a non-zero command passed" in prompt


def test_generate_test_plan_returns_repro_and_pass_steps(monkeypatch, tmp_path):
    import core

    monkeypatch.setattr(core, "run_configured_command", lambda *a, **k: None)
    monkeypatch.setattr(
        core, "load_json",
        lambda path: {"repro_steps": ["Open the page", "Click submit"], "pass_steps": ["Confirm no error"]},
    )
    plan = generate_test_plan("agent", tmp_path, {"number": 1, "title": "t"}, tmp_path / "plan.json", 10)
    assert plan == {
        "repro_steps": ["Open the page", "Click submit"],
        "pass_steps": ["Confirm no error"],
    }


def test_workflow_uses_only_cached_test_plan_and_formats_comment(tmp_path):
    class FakeStore:
        def __init__(self):
            self.saved = None

        def get_ticket_test(self, key):
            return {"repro_steps": ["Reproduce it"], "pass_steps": ["Verify it"]}

    class Ref:
        full_name = "acme/widgets"
        number = 42

    runner = WorkflowRunner(object(), FakeStore())
    plan = runner._get_cached_test_plan(Ref())
    assert plan == {"repro_steps": ["Reproduce it"], "pass_steps": ["Verify it"]}

    markdown = WorkflowRunner._format_test_plan_markdown(plan)
    assert "Steps to reproduce the original issue" in markdown
    assert "- [ ] Reproduce it" in markdown
    assert "Steps to verify the fix" in markdown


def test_workflow_does_not_generate_missing_test_plan():
    class FakeStore:
        def get_ticket_test(self, key):
            return None

    class Ref:
        full_name = "acme/widgets"
        number = 42

    runner = WorkflowRunner(object(), FakeStore())

    assert runner._get_cached_test_plan(Ref()) is None
