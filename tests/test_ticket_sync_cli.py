from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import ticket_sync


def test_find_gh_executable_reports_install_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ticket_sync, '_gh_candidates', lambda: [Path('missing-gh.exe')])

    with pytest.raises(RuntimeError, match='gh auth login'):
        ticket_sync.find_gh_executable()


def test_find_gh_executable_uses_existing_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / 'gh.exe'
    executable.write_text('placeholder', encoding='utf-8')
    monkeypatch.setattr(ticket_sync, '_gh_candidates', lambda: [executable])

    assert ticket_sync.find_gh_executable() == str(executable)


def test_sync_rejects_invalid_repository_before_running_gh() -> None:
    with pytest.raises(ValueError, match='owner/repository'):
        ticket_sync.sync_github('https://github.com/example/project')


def test_sync_excludes_pull_requests_before_loading_project_metadata(monkeypatch) -> None:
    issue = {
        'number': 1105, 'html_url': 'https://github.com/org/repo/issues/1105',
        'title': 'Backlogged issue', 'state': 'open', 'labels': [], 'assignees': [],
    }
    pull_request = {
        'number': 1106, 'html_url': 'https://github.com/org/repo/pull/1106',
        'title': 'A pull request', 'state': 'open', 'labels': [], 'assignees': [],
        'pull_request': {'url': 'https://api.github.com/repos/org/repo/pulls/1106'},
    }
    calls = []

    monkeypatch.setattr(
        ticket_sync,
        '_fetch_github_issues',
        lambda repository, state, limit, token: [issue, pull_request],
    )

    def fake_graphql(query, token):
        calls.append(query)
        assert 'i1105: issue(number: 1105)' in query
        assert '1106' not in query
        return {'data': {'repository': {'i1105': {'projectItems': {'nodes': [
            {'fieldValues': {'nodes': [
                {'name': 'Backlog', 'field': {'name': 'Status'}},
            ]}},
        ]}}}}}

    monkeypatch.setattr(ticket_sync, '_github_graphql', fake_graphql)

    tickets = ticket_sync.sync_github('org/repo', token='secret-token')

    assert [(ticket['number'], ticket['project_status']) for ticket in tickets] == [(1105, 'Backlog')]


def test_fetch_issues_uses_https_api_instead_of_parsing_gh_json(monkeypatch) -> None:
    issue = {'number': 7, 'title': 'Broken widget'}
    captured = {}

    class FakeResponse:
        status_code = 200
        text = json.dumps([issue])

        @staticmethod
        def json():
            return [issue]

    def fake_get(url, **kwargs):
        captured['url'] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(ticket_sync.requests, 'get', fake_get)

    result = ticket_sync._fetch_github_issues('org/repo', 'open', 100, 'secret-token')

    assert result == [issue]
    assert captured['url'] == 'https://api.github.com/repos/org/repo/issues'
    assert captured['headers']['Authorization'] == 'Bearer secret-token'
    assert captured['params'] == {'state': 'open', 'per_page': 100}


def test_missing_cli_token_has_actionable_error(monkeypatch) -> None:
    monkeypatch.setattr(
        ticket_sync,
        '_run_gh',
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, None, ''),
    )

    with pytest.raises(RuntimeError, match='Jack in // GitHub'):
        ticket_sync._github_token(None)


def test_404_error_explains_repository_access() -> None:
    result = subprocess.CompletedProcess(
        args=['gh', 'api'],
        returncode=1,
        stdout='',
        stderr='gh: Not Found (HTTP 404)',
    )

    error = ticket_sync._github_api_error('private-org/private-repo', result)

    assert 'could not access' in str(error)
    assert 'gh auth status' in str(error)
    assert 'SSO' in str(error)


def test_auth_error_explains_login() -> None:
    result = subprocess.CompletedProcess(
        args=['gh', 'api'],
        returncode=1,
        stdout='',
        stderr='You are not logged into any GitHub hosts.',
    )

    error = ticket_sync._github_api_error('owner/repo', result)

    assert 'gh auth login' in str(error)
