from __future__ import annotations

import json
import sqlite3
import statistics
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
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS job_log_chunks (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       job_id TEXT NOT NULL,
                       content TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                   )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_log_chunks_job_id_id "
                "ON job_log_chunks(job_id, id)"
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
            try:
                connection.execute("ALTER TABLE tickets ADD COLUMN has_attached_pr INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
            connection.execute("CREATE TABLE IF NOT EXISTS players (github_login TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            for column, definition in (
                ("historic_prs", "INTEGER"),
                ("progression_baseline_at", "TEXT"),
            ):
                try:
                    connection.execute(f"ALTER TABLE players ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS job_stage_timings (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       job_id TEXT NOT NULL,
                       stage TEXT NOT NULL,
                       duration_ms INTEGER NOT NULL,
                       created_at TEXT NOT NULL,
                       FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                   )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_stage_timings_stage "
                "ON job_stage_timings(stage)"
            )

    def upsert_tickets(self, tickets: list[dict]) -> None:
        with self._connect() as connection:
            for ticket in tickets:
                connection.execute(
                    """INSERT INTO tickets
                    (key, repository, number, url, title, state, labels, assignees,
                     priority, project_status, issue_type, created_at, updated_at, synced_at, source, has_attached_pr)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                      repository=excluded.repository, number=excluded.number, url=excluded.url,
                      title=excluded.title, state=excluded.state, labels=excluded.labels,
                      assignees=excluded.assignees,
                      priority=CASE WHEN excluded.source = 'github' THEN excluded.priority WHEN excluded.priority <> '' THEN excluded.priority ELSE tickets.priority END,
                      project_status=CASE WHEN excluded.source = 'github' THEN excluded.project_status WHEN excluded.project_status <> '' THEN excluded.project_status ELSE tickets.project_status END,
                      issue_type=excluded.issue_type,
                      created_at=excluded.created_at, updated_at=excluded.updated_at,
                      synced_at=excluded.synced_at, source=excluded.source,
                      has_attached_pr=excluded.has_attached_pr""",
                    tuple(ticket.get(field, '') for field in (
                        'key', 'repository', 'number', 'url', 'title', 'state', 'labels',
                        'assignees', 'priority', 'project_status', 'issue_type', 'created_at',
                        'updated_at', 'synced_at', 'source', 'has_attached_pr')),
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

    def list_testing_tickets(self, limit: int | None = None) -> list[dict]:
        """Return open tickets that are ready for integrity scanning."""
        query = "SELECT tickets.*, ticket_tests.repro_steps AS test_repro_steps, ticket_tests.pass_steps AS test_pass_steps FROM tickets LEFT JOIN ticket_tests ON ticket_tests.key = tickets.key WHERE upper(state) = 'OPEN' AND has_attached_pr = 1 AND lower(trim(project_status)) IN ('in progress', 'pr ready', 'ready for build')"
        params: list[object] = []
        query += " ORDER BY updated_at ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._connect() as connection:
            rows = [dict(row) for row in connection.execute(query, params).fetchall()]
        for row in rows:
            row['repro_steps'] = json.loads(row.pop('test_repro_steps') or '[]')
            row['pass_steps'] = json.loads(row.pop('test_pass_steps') or '[]')
        return rows

    def list_ticket_repositories(self) -> list[str]:
        """Return known repository slugs for direct ticket-number lookup."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT repository FROM tickets WHERE repository <> '' ORDER BY repository"
            ).fetchall()
        return [row['repository'] for row in rows]

    def list_ticket_references(self) -> list[dict]:
        """Return lightweight open-ticket references for the scanner search."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT repository, number, url, title FROM tickets "
                "WHERE upper(state) = 'OPEN' ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

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

    def get_player(self, login: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM players WHERE github_login = ?", (login,),
            ).fetchone()
        return dict(row) if row else None

    def initialize_player_progression(self, login: str, historic_prs: int) -> dict:
        """Store the immutable historic-PR baseline used before Contract rewards."""
        now = self._now()
        count = max(0, int(historic_prs))
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO players
                   (github_login, display_name, avatar_url, created_at, updated_at,
                    historic_prs, progression_baseline_at)
                   VALUES (?, ?, '', ?, ?, ?, ?)
                   ON CONFLICT(github_login) DO UPDATE SET
                     historic_prs=CASE
                       WHEN players.historic_prs IS NULL THEN excluded.historic_prs
                       ELSE players.historic_prs
                     END,
                     progression_baseline_at=CASE
                       WHEN players.progression_baseline_at IS NULL
                       THEN excluded.progression_baseline_at
                       ELSE players.progression_baseline_at
                     END,
                     updated_at=excluded.updated_at""",
                (login, login, now, now, count, now),
            )
            row = connection.execute(
                "SELECT * FROM players WHERE github_login = ?", (login,),
            ).fetchone()
        return dict(row)

    def leaderboard(self) -> list[dict]:
        with self._connect() as connection:
            players = {r['github_login']: dict(r) for r in connection.execute('SELECT * FROM players').fetchall()}
            jobs = [dict(row) for row in connection.execute(
                """SELECT github_login, status, issue_url, updated_at, parameters_json
                   FROM jobs WHERE github_login IS NOT NULL"""
            ).fetchall()]
        stats = {
            login: {**player, 'completed_contracts': set(), 'failed': 0}
            for login, player in players.items()
        }
        for job in jobs:
            login = job['github_login']
            item = stats.setdefault(login, {
                'github_login': login,
                'historic_prs': 0,
                'progression_baseline_at': None,
                'completed_contracts': set(),
                'failed': 0,
            })
            if job['status'] == 'failed':
                item['failed'] += 1
            if job['status'] != 'completed':
                continue
            parameters = json.loads(job['parameters_json'] or '{}')
            if parameters.get('workflow_profile') == 'testing_only':
                continue
            baseline = item.get('progression_baseline_at')
            if baseline and job['updated_at'] <= baseline:
                continue
            contract = (job['issue_url'] or '').strip().rstrip('/').lower()
            if contract:
                item['completed_contracts'].add(contract)

        result = []
        for item in stats.values():
            item['completed'] = len(item.pop('completed_contracts'))
            item['historic_prs'] = item.get('historic_prs') or 0
            item['xp'] = (item['historic_prs'] + item['completed']) * 120
            item['level'] = 1 + item['xp'] // 400
            result.append(item)
        return sorted(result, key=lambda item: (-item['xp'], item['github_login'].lower()))

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
        # Tool JSON and compiler diagnostics can arrive as a single enormous
        # line. Preserve useful edges without letting telemetry dominate SQLite.
        max_chars = 16_000
        if len(clean) > max_chars:
            marker = f"\n[telemetry compacted: {len(clean) - max_chars:,} characters omitted]\n"
            edge = (max_chars - len(marker)) // 2
            clean = clean[:edge] + marker + clean[-edge:]
        with self._lock:
            with self._connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if exists is None:
                    # A settings-level queue clear may race with a worker's
                    # final telemetry write; deleted jobs must stay deleted.
                    return
                connection.execute(
                    "INSERT INTO job_log_chunks(job_id, content, created_at) VALUES (?, ?, ?)",
                    (job_id, clean + "\n", self._now()),
                )
                connection.execute(
                    "UPDATE jobs SET updated_at = ? WHERE id = ?",
                    (self._now(), job_id),
                )

    def record_stage_timing(self, job_id: str, stage: str, duration_ms: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO job_stage_timings(job_id, stage, duration_ms, created_at) VALUES (?, ?, ?, ?)",
                (job_id, stage, duration_ms, self._now()),
            )

    def get_stage_timing_report(self) -> list[dict]:
        """Return per-stage count/avg/median/slowest duration_ms across all jobs."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT stage, duration_ms FROM job_stage_timings"
            ).fetchall()
        by_stage: dict[str, list[int]] = {}
        for row in rows:
            by_stage.setdefault(row["stage"], []).append(row["duration_ms"])
        report = []
        for stage, durations in by_stage.items():
            report.append({
                "stage": stage,
                "count": len(durations),
                "avg_ms": round(statistics.mean(durations)),
                "median_ms": round(statistics.median(durations)),
                "slowest_ms": max(durations),
            })
        return sorted(report, key=lambda item: item["avg_ms"], reverse=True)

    def request_approval(self, job_id: str, message: str) -> None:
        self.update(job_id, status="waiting_approval", approval_state="pending", approval_message=message)
        self.append_log(job_id, f"Approval required: {message}")

    def stop(self, job_id: str) -> None:
        self.update(job_id, status="stopped", approval_state="rejected", approval_message="")
        self.append_log(job_id, "Job stopped by user.")

    def clear_queue(self) -> int:
        """Remove every queued, active, and archived job from persistent storage."""
        with self._lock:
            with self._connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                # The FK cascade removes telemetry chunks with the jobs.
                connection.execute("DELETE FROM jobs")
        return count

    def get(self, job_id: str, *, include_logs: bool = False) -> dict | None:
        columns = "*" if include_logs else (
            "id, status, stage, issue_url, base_branch, parameters_json, result_json, "
            "error, created_at, updated_at, approval_state, approval_message, github_login"
        )
        with self._connect() as connection:
            row = connection.execute(f"SELECT {columns} FROM jobs WHERE id = ?", (job_id,)).fetchone()
            chunks = connection.execute(
                "SELECT id, content FROM job_log_chunks WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall() if row is not None and include_logs else []
        if row is None:
            return None
        result = dict(row)
        if include_logs:
            result["logs"] = (result.get("logs") or "") + "".join(chunk["content"] for chunk in chunks)
            result["log_cursor"] = chunks[-1]["id"] if chunks else 0
        result["parameters"] = json.loads(result.pop("parameters_json"))
        result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
        return result

    def get_with_logs(self, job_id: str) -> dict | None:
        return self.get(job_id, include_logs=True)

    def get_updates(self, job_id: str, log_cursor: int) -> dict | None:
        """Return job metadata plus only telemetry chunks newer than the cursor."""
        columns = (
            "id, status, stage, issue_url, base_branch, parameters_json, result_json, "
            "error, created_at, updated_at, approval_state, approval_message, github_login"
        )
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {columns} FROM jobs WHERE id = ?", (job_id,),
            ).fetchone()
            if row is None:
                return None
            latest = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM job_log_chunks WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            stale = log_cursor > latest
            chunks = connection.execute(
                "SELECT id, content FROM job_log_chunks WHERE job_id = ? AND id > ? ORDER BY id",
                (job_id, 0 if stale else max(0, log_cursor)),
            ).fetchall()
            legacy_logs = ""
            if stale:
                legacy_logs = connection.execute(
                    "SELECT logs FROM jobs WHERE id = ?", (job_id,),
                ).fetchone()[0] or ""
        result = dict(row)
        result["parameters"] = json.loads(result.pop("parameters_json"))
        result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
        result["logs_delta"] = legacy_logs + "".join(chunk["content"] for chunk in chunks)
        result["log_cursor"] = latest
        if stale:
            result["logs_reset"] = True
        return result

    def list(self, limit: int = 30) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, status, stage, issue_url, base_branch, parameters_json,
                          result_json, error, created_at, updated_at, approval_state,
                          approval_message, github_login, '' AS logs
                   FROM jobs ORDER BY created_at DESC LIMIT ?""", (limit,)
            ).fetchall()
        jobs = []
        for row in rows:
            item = dict(row)
            item["parameters"] = json.loads(item.pop("parameters_json"))
            item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
            jobs.append(item)
        return jobs

    def list_for_player(self, github_login: str) -> list[dict]:
        """Return the operator's full Contract history for progression totals."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, status, stage, issue_url, base_branch, parameters_json,
                          result_json, error, created_at, updated_at, approval_state,
                          approval_message, github_login, '' AS logs
                   FROM jobs WHERE github_login = ? ORDER BY created_at DESC""",
                (github_login,),
            ).fetchall()
        jobs = []
        for row in rows:
            item = dict(row)
            item["parameters"] = json.loads(item.pop("parameters_json"))
            item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
            jobs.append(item)
        return jobs

    def latest_for_issue(self, issue_url: str) -> dict | None:
        """Return the newest workflow run for an issue URL."""
        normalized = issue_url.strip().rstrip("/")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id, status, stage, issue_url, base_branch, parameters_json,
                          result_json, error, created_at, updated_at, approval_state,
                          approval_message, github_login, '' AS logs
                   FROM jobs WHERE rtrim(issue_url, '/') = ? ORDER BY created_at DESC LIMIT 1""",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["parameters"] = json.loads(item.pop("parameters_json"))
        item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
        return item
