import tempfile
import unittest
from pathlib import Path

import sad_hamster_bot as bot
from bot_settings import Settings


class BotContractTest(unittest.TestCase):
    def test_message_splitting_respects_discord_limit(self):
        chunks = bot.split_message("x" * 4001, 1900)
        self.assertEqual("".join(chunks), "x" * 4001)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))

    def test_settings_defaults_allow_long_running_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings("secret", 1, 2, root, root / "logs", "codex")
            self.assertEqual(settings.timeout, 0)
            self.assertEqual(settings.heartbeat_interval, 30)
