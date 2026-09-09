"""POSIX lifetime locks inherited by Codex to prevent overlapping orphan execution."""

import fcntl
import os


class RuntimeLease:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise ValueError(
                "Workspace/state is owned by another bot or Codex process. "
                "Wait for it to exit before restarting; do not delete its lock file."
            ) from None

    def close(self):
        if self.fd is not None:
            # Closing, not LOCK_UN, preserves the lease held by an inherited child descriptor.
            os.close(self.fd)
            self.fd = None
