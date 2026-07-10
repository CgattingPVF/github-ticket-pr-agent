import pytest
from pathlib import Path

from ticket_sync import import_workbook

from core import make_branch_name, parse_issue_url, parse_validation_commands, slugify, validate_ref_name


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


def test_validate_ref_rejects_option_like_names():
    with pytest.raises(ValueError):
        validate_ref_name("--upload-pack=evil")


def test_supplied_workbook_imports_github_tickets():
    tickets = import_workbook(Path('bug_tracker_items (1).xlsx'))
    assert len(tickets) > 100
    assert tickets[0]['repository'] == 'pvfscaffolding/crm-staff-desktop'
    assert tickets[0]['number'] == 1065
    assert tickets[0]['url'].endswith('/issues/1065')
