from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._init_db()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    issue_url TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    result_json TEXT,
                    logs TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            for column, definition in (
                ("approval_state", "TEXT NOT NULL DEFAULT 'auto'"),
                ("approval_message", "TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    key TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'OPEN',
                    labels TEXT NOT NULL DEFAULT '',
                    assignees TEXT NOT NULL DEFAULT '',
                    priority TEXT NOT NULL DEFAULT '',
                    project_status TEXT NOT NULL DEFAULT '',
                    issue_type TEXT NOT NULL DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT,
                    synced_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'github'
                )
                """
            )
            connection.execute("CREATE TABLE IF NOT EXISTS players (github_login TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_tests (
                    key TEXT PRIMARY KEY,
                    repro_steps TEXT NOT NULL DEFAULT '[]',
                    pass_steps TEXT NOT NULL DEFAULT '[]',
                    generated_at TEXT NOT NULL
                )
                """
            )
            try:
                connection.execute("ALTER TABLE jobs ADD COLUMN github_login TEXT")
            except sqlite3.OperationalError as exc:
                if 'duplicate column' not in str(exc).lower(): raise

    def upsert_tickets(self, tickets: list[dict]) -> None:
        with self._connect() as connection:
            for ticket in tickets:
                connection.execute(
                    """INSERT INTO tickets
                    (key, repository, number, url, title, state, labels, assignees,
                     priority, project_status, issue_type, created_at, updated_at, synced_at, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                      repository=excluded.repository, number=excluded.number, url=excluded.url,
                      title=excluded.title, state=excluded.state, labels=excluded.labels,
                      assignees=excluded.assignees,
                      priority=CASE WHEN excluded.source = 'github' THEN excluded.priority WHEN excluded.priority <> '' THEN excluded.priority ELSE tickets.priority END,
                      project_status=CASE WHEN excluded.source = 'github' THEN excluded.project_status WHEN excluded.project_status <> '' THEN excluded.project_status ELSE tickets.project_status END,
                      issue_type=excluded.issue_type,
                      created_at=excluded.created_at, updated_at=excluded.updated_at,
                      synced_at=excluded.synced_at, source=excluded.source""",
                    tuple(ticket.get(field, '') for field in (
                        'key', 'repository', 'number', 'url', 'title', 'state', 'labels',
                        'assignees', 'priority', 'project_status', 'issue_type', 'created_at',
                        'updated_at', 'synced_at', 'source')),
                )

    def list_tickets(self, limit: int | None = None, state: str = 'OPEN') -> list[dict]:
        query = "SELECT tickets.*, ticket_tests.repro_steps AS test_repro_steps, ticket_tests.pass_steps AS test_pass_steps FROM tickets LEFT JOIN ticket_tests ON ticket_tests.key = tickets.key"
        params: list[object] = []
        if state:
            query += " WHERE upper(state) = upper(?) AND replace(lower(trim(project_status)), '-', ' ') NOT LIKE 'in progress%' AND lower(trim(project_status)) NOT IN ('done', 'ready for build', 'closed', 'complete', 'completed', 'pr ready')"
            params.append(state)
        query += " ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 4 END, CASE WHEN lower(labels) LIKE '%regression%' THEN 0 WHEN lower(labels) LIKE '%bug%' THEN 1 ELSE 2 END, CASE lower(trim(project_status)) WHEN 'ready for build' THEN 0 WHEN 'in progress' THEN 1 ELSE 2 END, updated_at ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._connect() as connection:
            rows = [dict(row) for row in connection.execute(query, params).fetchall()]
        for row in rows:
            row['repro_steps'] = json.loads(row.pop('test_repro_steps') or '[]')
            row['pass_steps'] = json.loads(row.pop('test_pass_steps') or '[]')
        return rows

    def get_ticket_test(self, key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ticket_tests WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return {
            'repro_steps': json.loads(row['repro_steps'] or '[]'),
            'pass_steps': json.loads(row['pass_steps'] or '[]'),
            'generated_at': row['generated_at'],
        }

    def upsert_ticket_test(self, key: str, repro_steps: list[str], pass_steps: list[str]) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO ticket_tests (key, repro_steps, pass_steps, generated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  repro_steps=excluded.repro_steps, pass_steps=excluded.pass_steps, generated_at=excluded.generated_at""",
                (key, json.dumps(repro_steps), json.dumps(pass_steps), self._now()),
            )

    def prune_repository_tickets(self, repository: str, active_keys: list[str]) -> None:
        if not repository:
            return
        with self._connect() as connection:
            if active_keys:
                placeholders = ','.join('?' for _ in active_keys)
                connection.execute(
                    f"DELETE FROM tickets WHERE repository = ? AND key NOT IN ({placeholders})",
                    [repository, *active_keys],
                )
            else:
                connection.execute("DELETE FROM tickets WHERE repository = ?", (repository,))

    def create(self, parameters: dict) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, status, stage, issue_url, base_branch,
                    parameters_json, created_at, updated_at, github_login
                ) VALUES (?, 'queued', 'Queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    parameters["issue_url"],
                    parameters["base_branch"],
                    json.dumps(parameters),
                    now,
                    now,
                    parameters.get('github_login'),
                ),
            )
        return job_id

    def upsert_player(self, login: str, display_name: str = '', avatar_url: str = '') -> None:
        now = self._now()
        with self._connect() as connection:
            connection.execute("INSERT INTO players (github_login, display_name, avatar_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(github_login) DO UPDATE SET display_name=excluded.display_name, avatar_url=excluded.avatar_url, updated_at=excluded.updated_at", (login, display_name, avatar_url, now, now))

    def leaderboard(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT github_login, COUNT(CASE WHEN status='completed' THEN 1 END) completed, COUNT(CASE WHEN status='failed' THEN 1 END) failed FROM jobs WHERE github_login IS NOT NULL GROUP BY github_login ORDER BY (COUNT(CASE WHEN status='completed' THEN 1 END) * 120 + COUNT(CASE WHEN status='failed' THEN 1 END) * 20) DESC").fetchall()
            players = {r['github_login']: dict(r) for r in connection.execute('SELECT * FROM players').fetchall()}
        result = []
        for row in rows:
            item = {**players.get(row['github_login'], {}), **dict(row)}
            item['xp'] = item['completed'] * 120 + item['failed'] * 20
            item['level'] = 1 + item['xp'] // 400
            result.append(item)
        return result

    def update(self, job_id: str, **fields: object) -> None:
        if not fields:
            return
        fields["updated_at"] = self._now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [json.dumps(value) if key == "result_json" and value is not None else value for key, value in fields.items()]
        values.append(job_id)
        with self._connect() as connection:
            connection.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)

    def append_log(self, job_id: str, message: str) -> None:
        clean = message.rstrip()
        if not clean:
            return
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE jobs SET logs = logs || ?, updated_at = ? WHERE id = ?",
                    (clean + "\n", self._now(), job_id),
                )

    def request_approval(self, job_id: str, message: str) -> None:
        self.update(job_id, status="waiting_approval", approval_state="pending", approval_message=message)
        self.append_log(job_id, f"Approval required: {message}")

    def stop(self, job_id: str) -> None:
        self.update(job_id, status="stopped", approval_state="rejected", approval_message="")
        self.append_log(job_id, "Job stopped by user.")

    def get(self, job_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["parameters"] = json.loads(result.pop("parameters_json"))
        result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
        return result

    def list(self, limit: int = 30) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        jobs = []
        for row in rows:
            item = dict(row)
            item["parameters"] = json.loads(item.pop("parameters_json"))
            item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
            jobs.append(item)
        return jobs
