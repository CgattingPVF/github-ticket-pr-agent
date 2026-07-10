from __future__ import annotations

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
