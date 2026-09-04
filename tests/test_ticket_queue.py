from pathlib import Path

from store import JobStore


def test_in_progress_tickets_are_not_listed(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.upsert_tickets([
        {
            "key": "org#1029", "repository": "org/repo", "number": 1029,
            "url": "https://github.com/org/repo/issues/1029", "title": "In progress",
            "state": "OPEN", "project_status": "In Progress", "project_number": 11, "synced_at": "now",
        },
        {
            "key": "org#1030", "repository": "org/repo", "number": 1030,
            "url": "https://github.com/org/repo/issues/1030", "title": "Ready",
            "state": "OPEN", "project_status": "Backlog", "project_number": 11, "synced_at": "now",
        },
        {
            "key": "org#1031", "repository": "org/repo", "number": 1031,
            "url": "https://github.com/org/repo/issues/1031", "title": "In review",
            "state": "OPEN", "project_status": "To Triage", "project_number": 6, "synced_at": "now",
        },
    ])
    assert [ticket["number"] for ticket in store.list_tickets()] == [1030, 1031]


def test_ticket_queue_returns_only_intake_project_tickets_by_default(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    eligible = [
        {
            "key": f"org/repo#{number}",
            "repository": "org/repo",
            "number": number,
            "url": f"https://github.com/org/repo/issues/{number}",
            "title": f"Ticket {number}",
            "state": "OPEN",
            "project_status": "Intake" if number % 2 else "Backlog",
            "project_number": 11,
            "synced_at": "now",
        }
        for number in range(1, 76)
    ]
    in_progress = [
        {
            "key": f"org/repo#{number}",
            "repository": "org/repo",
            "number": number,
            "url": f"https://github.com/org/repo/issues/{number}",
            "title": f"Active ticket {number}",
            "state": "OPEN",
            "project_status": "In Progress - Development" if number % 2 else "In-Progress",
            "project_number": 11,
            "synced_at": "now",
        }
        for number in range(100, 116)
    ]
    store.upsert_tickets([*eligible, *in_progress])

    assert len(store.list_tickets()) == 75
    assert len(store.list_tickets(limit=20)) == 20


def test_integrity_scanner_lists_only_eligible_project_statuses(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.upsert_tickets([
        {"key": f"org/repo#{number}", "repository": "org/repo", "number": number,
         "url": f"https://github.com/org/repo/issues/{number}", "title": str(number),
         "state": "OPEN", "project_status": status, "has_attached_pr": has_pr, "synced_at": "now"}
        for number, status, has_pr in [(1, "In Progress", 1), (2, "PR Ready", 1), (3, "Ready for Build", 1), (4, "Backlog", 1), (5, "Done", 1), (6, "PR Ready", 0)]
    ])
    assert [ticket["number"] for ticket in store.list_testing_tickets()] == [1, 2, 3]
    assert store.list_ticket_repositories() == ["org/repo"]
    assert [ticket["number"] for ticket in store.list_ticket_references()] == [1, 2, 3, 4, 5, 6]


def test_github_resync_clears_stale_project_status(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    ticket = {
        "key": "org/repo#1105", "repository": "org/repo", "number": 1105,
        "url": "https://github.com/org/repo/issues/1105", "title": "Moved ticket",
        "state": "OPEN", "project_status": "In progress", "project_number": 11, "synced_at": "before",
        "source": "github",
    }
    store.upsert_tickets([ticket])
    assert store.list_tickets() == []

    store.upsert_tickets([{**ticket, "project_status": "Backlog", "synced_at": "after"}])

    assert store.list_tickets()[0]["number"] == 1105
