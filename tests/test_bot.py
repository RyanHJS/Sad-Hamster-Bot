"""Settings, path resolution, sessions, and the Discord authorization gate."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot
from sessions import Lease, Sessions
from settings import Settings


def make_settings(root, **overrides):
    values = dict(
        token="secret",
        user_id=123,
        base_folder=root,
        log_dir=root / "logs",
        state_dir=root / "state",
    )
    values.update(overrides)
    return Settings(**values)


class FolderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / "my-repo").mkdir()
        self.settings = make_settings(self.root)

    def test_valid_channel_maps_to_its_folder(self):
        self.assertEqual(self.settings.folder_for("my-repo"), self.root / "my-repo")

    def test_traversal_and_absolute_names_are_refused(self):
        for name in ("../escape", "..", ".", "/etc", "a/b", "a\\b", ".hidden", "", "   "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.settings.folder_for(name)

    def test_unmapped_channel_names_the_expected_folder(self):
        with self.assertRaisesRegex(ValueError, "No folder for #nope"):
            self.settings.folder_for("nope")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.env = self.root / ".env"
        self.env.write_text(
            "DISCORD_BOT_TOKEN=super-secret-token\nDISCORD_USER_ID=123\nBASE_FOLDER=.\n"
        )

    def load(self, **values):
        with (
            patch.dict(os.environ, values, clear=True),
            patch("settings.shutil.which", return_value="/usr/bin/agent"),
        ):
            return Settings.load(self.env)

    def test_defaults_allow_long_runs_and_pick_no_model(self):
        settings = self.load()
        self.assertEqual(settings.timeout, 0)
        self.assertEqual(settings.model, "")
        self.assertEqual(settings.reasoning, "")
        self.assertEqual(settings.agent, "codex")
        self.assertEqual(settings.base_folder, self.root)

    def test_the_token_never_appears_in_repr(self):
        settings = self.load()
        self.assertEqual(settings.token, "super-secret-token")
        self.assertNotIn("super-secret-token", repr(settings))

    def test_environment_overrides_the_env_file(self):
        self.assertEqual(self.load(DISCORD_USER_ID="789").user_id, 789)

    def test_invalid_settings_name_the_offending_key(self):
        for key, value in (
            ("DISCORD_BOT_TOKEN", ""),
            ("DISCORD_USER_ID", "abc"),
            ("DISCORD_USER_ID", "0"),
            ("BASE_FOLDER", "missing-dir"),
            ("AGENT", "gemini"),
            ("REASONING_EFFORT", "wrong"),
            ("MAX_RUN_SECONDS", "-1"),
            ("MAX_RUN_SECONDS", "nan"),
            ("DISCORD_CHANNEL_IDS", "abc"),
        ):
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                self.load(**{key: value})

    def test_channel_ids_parse_as_a_set(self):
        self.assertEqual(self.load(DISCORD_CHANNEL_IDS="12, 34 56").channels, {12, 34, 56})

    def test_missing_default_agent_binary_is_a_startup_error(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("settings.shutil.which", return_value=None),
            self.assertRaisesRegex(ValueError, "CODEX_BIN"),
        ):
            Settings.load(self.env)


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sessions = Sessions(self.root / "state")
        self.addCleanup(self.sessions.close)

    def test_sessions_persist_and_survive_reopening(self):
        self.sessions.remember(1, "sess-a", "codex")
        self.sessions.close()
        reopened = Sessions(self.root / "state")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(1)["session_id"], "sess-a")
        self.assertIsNone(reopened.get(999))

    def test_clear_keeps_the_agent_but_drops_the_session(self):
        self.sessions.remember(1, "sess-a", "claude")
        self.sessions.clear(1)
        row = self.sessions.get(1)
        self.assertIsNone(row["session_id"])
        self.assertEqual(row["agent"], "claude")

    def test_switching_agents_resets_the_session(self):
        self.sessions.remember(1, "sess-a", "codex")
        self.sessions.set_agent(1, "claude")
        row = self.sessions.get(1)
        self.assertEqual(row["agent"], "claude")
        self.assertIsNone(row["session_id"])

    def test_lease_excludes_a_second_owner_until_released(self):
        path = self.root / "state" / "runtime.lock"
        first = Lease(path)
        with self.assertRaises(ValueError):
            Lease(path)
        first.close()
        second = Lease(path)
        self.assertIsNotNone(second.fd)
        second.close()


class CommandTest(unittest.TestCase):
    def test_only_slash_prefixed_known_verbs_are_commands(self):
        self.assertEqual(bot.parse_command("/status"), ["status"])
        self.assertEqual(bot.parse_command("/agent claude"), ["agent", "claude"])
        for text in (
            "status of the migration - safe to deploy?",
            "clear the cache in redis",
            "stop the runaway job",
            "/deploy to prod",
            "/",
        ):
            with self.subTest(text=text):
                self.assertIsNone(bot.parse_command(text))

    def test_splitting_respects_the_discord_limit(self):
        chunks = bot.split_message("x" * 4001, 1900)
        self.assertEqual("".join(chunks), "x" * 4001)
        self.assertTrue(all(len(c) <= 1900 for c in chunks))

    def test_splitting_prefers_line_boundaries(self):
        text = "\n".join(["line " + "y" * 40] * 100)
        chunks = bot.split_message(text, 200)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertNotIn("\n\n", "".join(chunks))


class AuthorizationTest(unittest.TestCase):
    """The only thing standing between a Discord message and a local agent."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        settings = make_settings(root, channels=frozenset({456}))
        with patch.object(bot.discord.Client, "__init__", lambda self, **kw: None):
            self.client = bot.AgentBot(settings, sessions=object())

    def message(self, **overrides):
        values = dict(
            author=SimpleNamespace(id=123, bot=False),
            channel=SimpleNamespace(id=456, name="my-repo"),
            content="do a thing",
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_the_configured_user_in_a_listed_channel_is_allowed(self):
        self.assertTrue(self.client.authorized(self.message()))

    def test_everyone_and_everywhere_else_is_refused(self):
        cases = {
            "another user": self.message(author=SimpleNamespace(id=999, bot=False)),
            "a bot": self.message(author=SimpleNamespace(id=123, bot=True)),
            "an unlisted channel": self.message(channel=SimpleNamespace(id=1, name="x")),
            "a DM without a name": self.message(channel=SimpleNamespace(id=456, name=None)),
        }
        for label, message in cases.items():
            with self.subTest(case=label):
                self.assertFalse(self.client.authorized(message))

    def test_without_a_channel_allowlist_any_named_channel_passes(self):
        self.client.settings = make_settings(Path(self.directory.name))
        self.assertTrue(self.client.authorized(self.message(
            channel=SimpleNamespace(id=99, name="other-repo"))))
