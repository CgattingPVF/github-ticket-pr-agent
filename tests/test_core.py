import pytest
from pathlib import Path

from ticket_sync import import_workbook
from workflow import WorkflowRunner

from core import generate_test_plan, make_branch_name, parse_issue_url, parse_validation_commands, slugify, validate_ref_name
from prompts import all_in_one_prompt


def test_parse_issue_url():
    ref = parse_issue_url("https://github.com/acme/widgets/issues/123")
    assert ref.owner == "acme"
    assert ref.repo == "widgets"
    assert ref.number == 123


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


def test_workflow_test_plan_uses_cache_and_formats_comment(tmp_path):
    class FakeStore:
        def __init__(self):
            self.saved = None

        def get_ticket_test(self, key):
            return {"repro_steps": ["Reproduce it"], "pass_steps": ["Verify it"]}

        def upsert_ticket_test(self, key, repro_steps, pass_steps):
            self.saved = (key, repro_steps, pass_steps)

    class FakeSettings:
        workspace_root = tmp_path
        review_timeout_seconds = 10

    class Ref:
        full_name = "acme/widgets"
        number = 42

    runner = WorkflowRunner(FakeSettings(), FakeStore())
    plan = runner._get_or_generate_test_plan(Ref(), {"number": 42}, "agent", tmp_path, lambda m: None)
    assert plan == {"repro_steps": ["Reproduce it"], "pass_steps": ["Verify it"]}

    markdown = WorkflowRunner._format_test_plan_markdown(plan)
    assert "Steps to reproduce the original issue" in markdown
    assert "- [ ] Reproduce it" in markdown
    assert "Steps to verify the fix" in markdown
