"""Durable job metadata. Prompts are never stored or replayed by this module."""

import math
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

_ACTIVE = "('accepted', 'starting', 'running', 'cancelling')"
_JOB_FIELDS = frozenset(
    {"state", "result", "status_message_id", "delivery", "elapsed", "activity", "pid"}
)
_ATTEMPT_FIELDS = frozenset({"outcome", "elapsed", "detail", "tokens", "reported_model"})


class JobStore:
    """Synchronous SQLite store, intended for one owning application thread.

    Times are Unix seconds. Missing mutation targets raise ValueError;
    duplicate messages and workspace conflicts raise sqlite3.IntegrityError.
    Opening a store never reconciles or resumes work.
    """

    def __init__(self, directory: Path):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        database = directory / "jobs.sqlite3"
        descriptor = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self._connection = sqlite3.connect(database, timeout=10)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA secure_delete = ON")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported job store schema version: {version}")
            self._connection.executescript(f"""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    label TEXT NOT NULL,
                    codex_id TEXT,
                    created REAL NOT NULL,
                    UNIQUE(scope, id)
                );
                CREATE TABLE IF NOT EXISTS selections (
                    scope TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    FOREIGN KEY(scope, session_id) REFERENCES sessions(scope, id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'accepted',
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    result TEXT,
                    status_message_id INTEGER,
                    delivery TEXT NOT NULL DEFAULT 'pending',
                    elapsed REAL NOT NULL DEFAULT 0,
                    activity TEXT,
                    pid INTEGER,
                    FOREIGN KEY(scope, session_id) REFERENCES sessions(scope, id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_workspace
                    ON jobs(workspace) WHERE state IN {_ACTIVE};
                CREATE INDEX IF NOT EXISTS jobs_scope_created ON jobs(scope, created);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    number INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'running',
                    outcome TEXT,
                    elapsed REAL NOT NULL DEFAULT 0,
                    detail TEXT,
                    tokens,
                    reported_model TEXT,
                    UNIQUE(job_id, number)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY(job_id, chunk_index)
                );
                PRAGMA user_version = 1;
                COMMIT;
            """)
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def _one(self, query: str, parameters: tuple = ()) -> dict | None:
        row = self._connection.execute(query, parameters).fetchone()
        return dict(row) if row is not None else None

    def _all(self, query: str, parameters: tuple = ()) -> list[dict]:
        return [dict(row) for row in self._connection.execute(query, parameters)]

    def _session(self, scope: str, session_id: str) -> dict:
        session = self._one(
            "SELECT * FROM sessions WHERE scope = ? AND id = ?", (scope, session_id)
        )
        if session is None:
            raise ValueError("Session does not exist in this scope")
        return session

    def _insert_session(self, scope: str, label: str) -> dict:
        session_id = uuid4().hex
        self._connection.execute(
            "INSERT INTO sessions(id, scope, label, created) VALUES (?, ?, ?, ?)",
            (session_id, scope, label, time.time()),
        )
        return self._session(scope, session_id)

    def create_session(self, scope: str, label: str = "") -> dict:
        with self._connection:
            return self._insert_session(scope, label)

    def sessions(self, scope: str) -> list[dict]:
        return self._all("SELECT * FROM sessions WHERE scope = ? ORDER BY created, rowid", (scope,))

    def _select(self, scope: str, session_id: str) -> None:
        self._connection.execute(
            "INSERT INTO selections(scope, session_id) VALUES (?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET session_id = excluded.session_id",
            (scope, session_id),
        )

    def select_session(self, scope: str, session_id: str) -> dict:
        with self._connection:
            session = self._session(scope, session_id)
            self._select(scope, session_id)
            return session

    def current_session(self, scope: str) -> dict | None:
        return self._one(
            "SELECT s.* FROM sessions s JOIN selections c "
            "ON s.scope = c.scope AND s.id = c.session_id WHERE c.scope = ?",
            (scope,),
        )

    def new_session(self, scope: str, label: str = "") -> dict:
        with self._connection:
            session = self._insert_session(scope, label)
            self._select(scope, session["id"])
            return session

    def set_codex_id(self, session_id: str, codex_id: str) -> None:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE sessions SET codex_id = ? WHERE id = ?", (codex_id, session_id)
            )
            if not cursor.rowcount:
                raise ValueError("Unknown session ID")

    def create_job(self, scope: str, workspace: str, message_id: int, session_id: str) -> dict:
        job_id, now = uuid4().hex, time.time()
        with self._connection:
            self._session(scope, session_id)
            self._connection.execute(
                "INSERT INTO jobs(id, scope, workspace, source_message_id, session_id, "
                "created, updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, scope, str(workspace), message_id, session_id, now, now),
            )
            return self._one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def active_job(self, workspace: str) -> dict | None:
        return self._one(
            f"SELECT * FROM jobs WHERE workspace = ? AND state IN {_ACTIVE}", (str(workspace),)
        )

    def get_job(self, scope: str, job_id: str | None = None) -> dict | None:
        if job_id is not None:
            return self._one("SELECT * FROM jobs WHERE scope = ? AND id = ?", (scope, job_id))
        return self._one(
            "SELECT * FROM jobs WHERE scope = ? ORDER BY created DESC, rowid DESC LIMIT 1",
            (scope,),
        )

    def get_job_by_message(self, scope: str, message_id: int) -> dict | None:
        return self._one(
            "SELECT * FROM jobs WHERE scope = ? AND source_message_id = ?",
            (scope, message_id),
        )

    def _update(self, table: str, record_id: str, fields: dict) -> dict:
        with self._connection:
            if fields:
                assignments = ", ".join(f"{field} = ?" for field in fields)
                self._connection.execute(
                    f"UPDATE {table} SET {assignments} WHERE id = ?", (*fields.values(), record_id)
                )
            record = self._one(f"SELECT * FROM {table} WHERE id = ?", (record_id,))
            if record is None:
                raise ValueError(f"Unknown {table} ID")
            return record

    def update_job(self, job_id: str, **fields) -> dict:
        if fields.keys() - _JOB_FIELDS:
            raise ValueError("Unsupported job update fields")
        return self._update("jobs", job_id, {**fields, "updated": time.time()})

    def attempts(self, job_id: str) -> list[dict]:
        return self._all("SELECT * FROM attempts WHERE job_id = ? ORDER BY number", (job_id,))

    def add_attempt(self, job_id: str, model: str, reasoning: str) -> dict:
        attempt_id = uuid4().hex
        with self._connection:
            # Allocate and insert the next number in one serialized write statement.
            self._connection.execute(
                "INSERT INTO attempts(id, job_id, number, model, reasoning) "
                "SELECT ?, ?, COALESCE(MAX(number), 0) + 1, ?, ? FROM attempts WHERE job_id = ?",
                (attempt_id, job_id, model, reasoning, job_id),
            )
            return self._one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))

    def update_attempt(self, attempt_id: str, **fields) -> dict:
        if fields.keys() - _ATTEMPT_FIELDS:
            raise ValueError("Unsupported attempt update fields")
        if "outcome" in fields:
            fields["state"] = fields["outcome"]
        return self._update("attempts", attempt_id, fields)

    def pending_deliveries(self, scope: str) -> list[dict]:
        """Return recoverable deliveries; uncertain sends require inspection before retry."""
        return self._all(
            f"SELECT * FROM jobs WHERE scope = ? AND state NOT IN {_ACTIVE} "
            "AND delivery IN ('pending', 'retry', 'uncertain') ORDER BY created, rowid",
            (scope,),
        )

    def record_chunk(self, job_id: str, index: int, message_id: int) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO chunks(job_id, chunk_index, message_id) VALUES (?, ?, ?) "
                "ON CONFLICT(job_id, chunk_index) DO UPDATE SET message_id = excluded.message_id",
                (job_id, index, message_id),
            )

    def delivered_chunks(self, job_id: str) -> dict[int, int]:
        return dict(
            self._connection.execute(
                "SELECT chunk_index, message_id FROM chunks WHERE job_id = ? ORDER BY chunk_index",
                (job_id,),
            ).fetchall()
        )

    def reconcile_interrupted(self) -> int:
        """Call only after acquiring the exclusive OS lock for the workspace(s).

        Marks abandoned work for result delivery; never starts a process or replays a prompt.
        Terminal sends become uncertain, not automatically retryable. Returns the number
        of active jobs interrupted, excluding delivery-only changes.
        """
        with self._connection:
            self._connection.execute(
                "UPDATE jobs SET delivery = 'uncertain' "
                f"WHERE delivery = 'sending' AND state NOT IN {_ACTIVE}"
            )
            self._connection.execute(
                "UPDATE attempts SET state = 'interrupted', outcome = 'interrupted' "
                f"WHERE state = 'running' AND job_id IN (SELECT id FROM jobs WHERE state IN {_ACTIVE})"
            )
            cursor = self._connection.execute(
                "UPDATE jobs SET state = 'interrupted', result = ?, delivery = 'pending', "
                f"updated = ?, pid = NULL WHERE state IN {_ACTIVE}",
                ("Job interrupted by a bot restart. The request was not replayed.", time.time()),
            )
            return cursor.rowcount

    def prune(self, days: float = 7) -> int:
        """Expire terminal payloads; retain job IDs to prevent source-message replay."""
        if not math.isfinite(days) or days < 0:
            raise ValueError("Pruning days must be finite and nonnegative")
        cutoff = time.time() - days * 86400
        eligible = f"SELECT id FROM jobs WHERE state NOT IN {_ACTIVE} AND updated < ?"
        with self._connection:
            self._connection.execute(f"DELETE FROM chunks WHERE job_id IN ({eligible})", (cutoff,))
            self._connection.execute(
                f"DELETE FROM attempts WHERE job_id IN ({eligible})", (cutoff,)
            )
            cursor = self._connection.execute(
                "UPDATE jobs SET result = NULL, activity = NULL, status_message_id = NULL, "
                f"delivery = 'expired' WHERE id IN ({eligible}) "
                "AND (delivery != 'expired' OR result IS NOT NULL OR activity IS NOT NULL "
                "OR status_message_id IS NOT NULL)",
                (cutoff,),
            )
            return cursor.rowcount
