import asyncio
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import sad_hamster_bot as bot


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.env = self.root / ".env"
        self.env.write_text(
            "DISCORD_BOT_TOKEN=test-secret\nDISCORD_USER_ID=123\n"
            "DISCORD_CHANNEL_ID=456\nCODEX_WORKDIR=.\nCODEX_BIN=codex\n"
        )

    def load(self, **values):
        with (
            patch.dict(os.environ, values, clear=True),
            patch.object(bot.shutil, "which", return_value="/usr/bin/codex"),
        ):
            return bot.Settings.load(self.env)

    def test_dotenv_and_relative_paths(self):
        settings = self.load()
        self.assertEqual(settings.workdir, self.root)
        self.assertEqual(settings.log_dir, self.root / "logs")
        self.assertEqual(settings.user_id, 123)
        self.assertNotIn("test-secret", repr(settings))

    def test_environment_overrides_dotenv(self):
        self.assertEqual(self.load(DISCORD_USER_ID="789").user_id, 789)

    def test_invalid_settings_fail_at_startup(self):
        for key, value in (
            ("DISCORD_BOT_TOKEN", ""),
            ("DISCORD_USER_ID", "abc"),
            ("DISCORD_CHANNEL_ID", "0"),
            ("CODEX_TIMEOUT_SECONDS", "nan"),
            ("CODEX_TIMEOUT_SECONDS", "inf"),
            ("CODEX_TIMEOUT_SECONDS", "-1"),
            ("CODEX_TIMEOUT_SECONDS", ""),
            ("CODEX_TIMEOUT_SECONDS", "abc"),
            ("CODEX_WORKDIR", "missing-directory"),
            ("CODEX_REASONING_EFFORT", "wrong"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.load(**{key: value})

    def test_missing_executable_fails_at_startup(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(bot.shutil, "which", return_value=None),
            self.assertRaisesRegex(ValueError, "CODEX_BIN"),
        ):
            bot.Settings.load(self.env)

    def test_bare_timeout_setting_reports_configuration_error(self):
        with self.env.open("a") as file:
            file.write("CODEX_TIMEOUT_SECONDS\n")
        with self.assertRaisesRegex(ValueError, "CODEX_TIMEOUT_SECONDS"):
            self.load()


class LoggingTest(unittest.TestCase):
    def test_each_launch_gets_a_separate_log_without_duplicate_handlers(self):
        root = logging.getLogger()
        original_handlers, original_level = root.handlers[:], root.level
        root.handlers.clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                first = bot.configure_logging(Path(directory))
                bot.log.info("first launch")
                second = bot.configure_logging(Path(directory))
                bot.log.info("second launch")
                self.assertNotEqual(first, second)
                self.assertIn("first launch", first.read_text())
                self.assertNotIn("second launch", first.read_text())
                self.assertIn("second launch", second.read_text())
                self.assertEqual(len(root.handlers), 2)
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers[:] = original_handlers
            root.setLevel(original_level)


def settings(root=Path("/tmp"), **overrides):
    return bot.Settings(
        token="secret",
        user_id=123,
        channel_id=456,
        workdir=root,
        log_dir=root / "logs",
        codex_bin="codex",
        **overrides,
    )


class CodexTest(unittest.IsolatedAsyncioTestCase):
    async def test_clean_response_and_safe_prompt_transport(self):
        process = AsyncMock()
        process.pid = 999999
        process.stdin = Mock()
        process.returncode = 0
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(b"The actual answer.")
        process.stdout.feed_eof()
        process.stderr = asyncio.StreamReader()
        process.stderr.feed_data(
            b"OpenAI Codex v1\n--------\nworkdir: /private/work\nmodel: gpt-5\n"
            b"reasoning effort: high\nsession id: secret\n--------\n"
            b"user\nMy prompt\ncodex\nThe actual answer.\ntokens used\n1,234\n"
        )
        process.stderr.feed_eof()
        with (
            patch.object(bot.asyncio, "create_subprocess_exec", return_value=process) as start,
            patch.object(bot.os, "killpg"),
            patch.object(bot, "monotonic", side_effect=[10.0, 12.5]),
        ):
            result = await bot.run_codex("--help", settings(model="gpt-5", reasoning="high"))
        self.assertEqual(start.call_args.args[-1], "-")
        self.assertEqual(
            start.call_args.args[-5:-1],
            (
                "--model",
                "gpt-5",
                "-c",
                'model_reasoning_effort="high"',
            ),
        )
        self.assertNotIn("--help", start.call_args.args)
        process.stdin.write.assert_called_once_with(b"--help")
        self.assertNotIn("DISCORD_BOT_TOKEN", start.call_args.kwargs["env"])
        self.assertTrue(start.call_args.kwargs["start_new_session"])
        self.assertEqual(
            result.format(123),
            (
                "<@123>\n**Model:** gpt-5 | **Reasoning:** high | "
                "**Tokens:** 1,234 | **Time:** 2.5s\n\nThe actual answer."
            ),
        )

    async def test_missing_executable_is_a_clean_error(self):
        with patch.object(bot.asyncio, "create_subprocess_exec", side_effect=OSError("secret")):
            result = await bot.run_codex("hi", settings())
        self.assertEqual(
            result.message, "Codex could not start. Check its executable and workspace."
        )

    async def test_output_limit(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 100)
        stream.feed_eof()
        with self.assertRaises(bot.OutputLimitExceeded):
            await bot.read_output(stream, limit=50)


class DiscordTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = bot.CodexBot(settings())
        self.client._connection.user = SimpleNamespace(id=789)
        self.message = SimpleNamespace(
            id=1,
            author=SimpleNamespace(id=123, bot=False),
            channel=SimpleNamespace(
                id=456, send=AsyncMock(), typing=Mock(return_value=AsyncMock())
            ),
            mentions=[SimpleNamespace(id=789)],
            content="<@789> hi",
        )

    async def test_long_reply_mentions_author_only_once(self):
        result = bot.CodexResult("x" * 4000, 1.0)
        with patch.object(bot, "run_codex", return_value=result):
            await self.client.on_message(self.message)
        calls = self.message.channel.send.call_args_list
        self.assertEqual("".join(call.args[0] for call in calls), result.format(123))
        self.assertTrue(all(len(call.args[0]) <= 1900 for call in calls))
        self.assertEqual(calls[0].kwargs["allowed_mentions"].users, [self.message.author])
        for call in calls[1:]:
            self.assertFalse(call.kwargs["allowed_mentions"].users)
        for call in calls:
            self.assertFalse(call.kwargs["allowed_mentions"].everyone)
            self.assertFalse(call.kwargs["allowed_mentions"].roles)

    async def test_unauthorized_messages_are_ignored(self):
        for target, key, value in (
            (self.message.author, "id", 999),
            (self.message.author, "bot", True),
            (self.message.channel, "id", 999),
            (self.message, "mentions", []),
        ):
            with patch.object(target, key, value), patch.object(bot, "run_codex") as run:
                await self.client.on_message(self.message)
                run.assert_not_called()
        self.message.channel.send.assert_not_called()

    async def test_empty_and_busy_requests_do_not_start_codex(self):
        with patch.object(bot, "run_codex") as run:
            self.message.content = "<@!789>"
            await self.client.on_message(self.message)
            self.assertIn("include a request", self.message.channel.send.call_args.args[0])
            self.message.content = "<@789> hi"
            async with self.client.lock:
                await self.client.on_message(self.message)
            self.assertIn("already working", self.message.channel.send.call_args.args[0])
            run.assert_not_called()

    async def test_unexpected_failure_releases_lock_and_notifies_user(self):
        with patch.object(bot, "run_codex", side_effect=RuntimeError("private text")):
            await self.client.on_message(self.message)
        self.assertFalse(self.client.lock.locked())
        reply = self.message.channel.send.call_args.args[0]
        self.assertIn("<@123>", reply)
        self.assertNotIn("private text", reply)

    async def test_delivery_failure_releases_lock(self):
        failure = bot.discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "")
        self.message.channel.send.side_effect = failure
        with patch.object(bot, "run_codex", return_value=bot.CodexResult("hi", 1)):
            await self.client.on_message(self.message)
            self.assertFalse(self.client.lock.locked())
            self.message.channel.send.side_effect = None
            await self.client.on_message(self.message)
        self.assertEqual(self.message.channel.send.call_count, 2)


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def executable(self, source):
        executable = self.root / "fake-codex"
        executable.write_text(f"#!{sys.executable}\n{source}")
        executable.chmod(0o700)
        return str(executable)

    def config(self, source, **overrides):
        return bot.Settings(
            token="secret",
            user_id=123,
            channel_id=456,
            workdir=self.root,
            log_dir=self.root,
            codex_bin=self.executable(source),
            **overrides,
        )

    async def test_actual_process_stdin_and_missing_metadata(self):
        config = self.config("import sys\nprint(sys.stdin.read())\n")
        result = await bot.run_codex("--help\nUnicode: \u00e9", config)
        self.assertEqual(result.message, "--help\nUnicode: \u00e9")
        self.assertEqual(result.tokens, "unavailable")
        self.assertEqual(result.model, "unavailable")

    async def test_empty_output(self):
        result = await bot.run_codex("hi", self.config("pass\n"))
        self.assertEqual(result.message, "Codex completed without output.")

    async def test_failure_does_not_log_transcript(self):
        config = self.config("import sys\nprint('private prompt', file=sys.stderr)\nsys.exit(2)\n")
        with self.assertLogs(bot.log, level="INFO") as logs:
            result = await bot.run_codex("hi", config)
        self.assertIn("status 2", result.message)
        self.assertNotIn("private prompt", "".join(logs.output) + result.message)

    async def test_output_limit_stops_real_process(self):
        config = self.config("import sys, time\nsys.stdout.write('x' * 1100000)\ntime.sleep(30)\n")
        result = await asyncio.wait_for(bot.run_codex("hi", config), 5)
        self.assertIn("output exceeded", result.message)

    async def test_timeout_and_cancellation_stop_child_tools(self):
        source = (
            "import subprocess, sys, time\nfrom pathlib import Path\n"
            "subprocess.Popen([sys.executable, '-c', "
            "\"import time; from pathlib import Path; time.sleep(4); Path('escaped').touch()\"])\n"
            "Path('started').touch()\ntime.sleep(30)\n"
        )
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                (self.root / "started").unlink(missing_ok=True)
                config = self.config(source, timeout=2 if not cancel else 30)
                task = asyncio.create_task(bot.run_codex("hi", config))
                async with asyncio.timeout(5):
                    while not (self.root / "started").exists():
                        await asyncio.sleep(0.01)
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        self.assertEqual((await task).message, "Codex timed out.")
                await asyncio.sleep(4.1)
                self.assertFalse((self.root / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
