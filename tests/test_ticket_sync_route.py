from __future__ import annotations

import app as app_module


def test_ticket_sync_reports_authentication_failure_as_401(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        'sync_github',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError('GitHub CLI did not provide an authentication token.')
        ),
    )
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().post(
        '/tickets/sync',
        json={'repository': 'org/repo'},
    )

    assert response.status_code == 401
    assert response.get_json()['kind'] == 'authentication'


def test_ticket_sync_reports_invalid_repository_as_422(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        'sync_github',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError('Repository must use the `owner/repository` format.')
        ),
    )
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().post(
        '/tickets/sync',
        json={'repository': 'https://github.com/org/repo'},
    )

    assert response.status_code == 422
    assert response.get_json()['kind'] == 'validation'


def test_ticket_sync_combines_both_crm_repositories(monkeypatch) -> None:
    calls = []
    pruned = []
    saved = []

    def fake_sync(repository, token=None):
        calls.append(repository)
        number = 7 if repository.endswith('crm-staff-desktop') else 8
        return [{
            'key': f'{repository}#{number}',
            'repository': repository,
            'number': number,
        }]

    class FakeStore:
        def prune_repository_tickets(self, repository, keys):
            pruned.append((repository, keys))

        def upsert_tickets(self, tickets):
            saved.extend(tickets)

        def list_tickets(self):
            return saved

    monkeypatch.setattr(app_module, 'sync_github', fake_sync)
    monkeypatch.setattr(app_module, 'store', FakeStore())
    monkeypatch.setattr(app_module, 'get_github_token', lambda: 'token')
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().post(
        '/tickets/sync',
        json={'repositories': [
            'pvfscaffolding/crm-staff-desktop',
            'pvfscaffolding/crm-api',
        ]},
    )

    assert response.status_code == 200
    assert calls == [
        'pvfscaffolding/crm-staff-desktop',
        'pvfscaffolding/crm-api',
    ]
    assert len(pruned) == 2
    assert response.get_json()['count'] == 2
    assert response.get_json()['synced_count'] == 2
