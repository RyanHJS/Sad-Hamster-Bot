import tempfile
import unittest
from pathlib import Path

from runtime_lock import RuntimeLease


class LeaseTest(unittest.TestCase):
    def test_competing_process_owner_cannot_acquire_then_can_after_close(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'bot.lock'
            owner = RuntimeLease(path)
            try:
                with self.assertRaisesRegex(ValueError, 'another bot or Codex'):
                    RuntimeLease(path)
            finally:
                owner.close()
            next_owner = RuntimeLease(path)
            next_owner.close()
