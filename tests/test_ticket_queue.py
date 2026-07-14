from pathlib import Path

from store import JobStore


def test_in_progress_tickets_are_not_listed(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.upsert_tickets([
        {
            "key": "org#1029", "repository": "org/repo", "number": 1029,
            "url": "https://github.com/org/repo/issues/1029", "title": "In progress",
            "state": "OPEN", "project_status": "In Progress", "synced_at": "now",
        },
        {
            "key": "org#1030", "repository": "org/repo", "number": 1030,
            "url": "https://github.com/org/repo/issues/1030", "title": "Ready",
            "state": "OPEN", "project_status": "Backlog", "synced_at": "now",
        },
    ])
    assert [ticket["number"] for ticket in store.list_tickets()] == [1030]
