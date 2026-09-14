"""Remember which agent session belongs to which Discord channel.

One row per channel is the whole design. Nothing here records in-flight work, so
a crash cannot leave a job wedged: the next message simply resumes the session.
"""

import fcntl
import os
import sqlite3
import time
from pathlib import Path


class Sessions:
    def __init__(self, directory: Path):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        database = directory / "sessions.sqlite3"
        # O_NOFOLLOW and 0600: this file names the agent sessions for your repos.
        fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self._connection = sqlite3.connect(database, timeout=10)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS channels (
                   channel_id TEXT PRIMARY KEY,
                   session_id TEXT,
                   agent      TEXT NOT NULL,
                   updated    REAL NOT NULL
               )"""
        )
        self._connection.commit()

    def get(self, channel_id) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM channels WHERE channel_id = ?", (str(channel_id),)
        ).fetchone()
        return dict(row) if row else None

    def remember(self, channel_id, session_id, agent) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO channels(channel_id, session_id, agent, updated) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET "
                "session_id = excluded.session_id, agent = excluded.agent, "
                "updated = excluded.updated",
                (str(channel_id), session_id, agent, time.time()),
            )

    def set_agent(self, channel_id, agent) -> None:
        """Switch agents, dropping the session id: they are not interchangeable."""
        with self._connection:
            self._connection.execute(
                "INSERT INTO channels(channel_id, session_id, agent, updated) "
                "VALUES (?, NULL, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET "
                "agent = excluded.agent, session_id = NULL, updated = excluded.updated",
                (str(channel_id), agent, time.time()),
            )

    def clear(self, channel_id) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE channels SET session_id = NULL, updated = ? WHERE channel_id = ?",
                (time.time(), str(channel_id)),
            )

    def close(self) -> None:
        self._connection.close()


class Lease:
    """An exclusive lock on the state directory, so two bots cannot share it.

    Closing rather than unlocking is deliberate: a descriptor inherited by a
    child keeps the lease alive while that child still runs.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise ValueError(
                "Another bot already owns this state directory. Stop it first; "
                "do not delete its lock file."
            ) from None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
