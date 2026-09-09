import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import codex_runner as runner


class RunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_does_not_hide_permission_denial_for_live_child(self):
        process = SimpleNamespace(
            pid=123,
            returncode=None,
            stdout=asyncio.StreamReader(),
            stderr=asyncio.StreamReader(),
            wait=AsyncMock(),
        )
        with patch.object(runner.os, "killpg", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                await runner._cleanup(process, 0.05)
        process.wait.assert_not_awaited()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def config(self, source, **kwargs):
        exe = self.root / "codex"
        exe.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n" + source)
        exe.chmod(0o700)
        values = dict(
            codex_bin=str(exe),
            workdir=self.root,
            model="test-model",
            reasoning="high",
            timeout=0,
            output_limit=1024 * 1024,
        )
        values.update(kwargs)
        return SimpleNamespace(**values)

    def emit(self, *events):
        return "".join(f"print({json.dumps(event)!r}, flush=True)\n" for event in events)

    async def run_cli(self, source, **kwargs):
        return await runner.run_codex("hello", self.config(source, **kwargs), terminate_grace=0.05)

    async def test_stdin_resume_environment_and_metadata(self):
        source = (
            "assert sys.argv[1:] == ['exec', 'resume', '--json', '--skip-git-repo-check', "
            "'--model', 'test-model', '-c', 'model_reasoning_effort=\"high\"', 'session-123', '-']\n"
            "assert not any(k.startswith('DISCORD_') for k in os.environ)\n"
            "text = sys.stdin.read()\n"
            "print(json.dumps({'type':'thread.started','thread_id':'session-123'}), flush=True)\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))\n"
        ) + self.emit({"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 7}})
        events = []
        with patch.dict(os.environ, DISCORD_SECRET="private"):
            result = await runner.run_codex(
                "--help\n$(touch injected)\n\u00e9",
                self.config(source),
                "session-123",
                events.append,
            )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.message, "--help\n$(touch injected)\n\u00e9")
        self.assertEqual(
            (result.model, result.reasoning, result.tokens, result.session_id),
            ("test-model", "high", 11, "session-123"),
        )
        self.assertEqual(events[0]["kind"], "started")
        self.assertIsInstance(events[0]["pid"], int)
        self.assertIn({"kind": "session", "session_id": "session-123"}, events)
        self.assertFalse((self.root / "injected").exists())

    async def test_progress_is_early_private_and_quiet_is_not_timeout(self):
        ready = asyncio.Event()
        events = []

        def event(value):
            events.append(value)
            if value["kind"] == "activity":
                ready.set()

        source = (
            self.emit(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution", "command": "SECRET"},
                },
                {"type": "error", "message": "SECRET"},
            )
            + "time.sleep(0.2)\n"
            + self.emit(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
                {"type": "turn.completed"},
            )
        )
        task = asyncio.create_task(runner.run_codex("hi", self.config(source), on_event=event))
        await asyncio.wait_for(ready.wait(), 2)
        self.assertFalse(task.done())
        self.assertEqual((await task).state, "succeeded")
        self.assertNotIn("SECRET", repr(events))
        self.assertTrue(any(e["kind"] == "warning" for e in events))

    async def test_fragmented_unicode_and_unknown_events(self):
        data = (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "\u00e9\U0001f642"},
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode()
        source = self.emit(
            {"type": "future.event"}, {"type": "item.updated", "item": {"type": "future_item"}}
        )
        source += f"for b in {data!r}:\n os.write(1, bytes([b]))\n"
        source += self.emit({"type": "turn.completed"})
        result = await self.run_cli(source)
        self.assertEqual(result.message, "\u00e9\U0001f642")
        self.assertEqual(result.state, "succeeded")

    async def test_completion_requires_zero_exit(self):
        for source, expected in [
            ("pass\n", "protocol_error"),
            ("sys.stderr.write('SECRET'); sys.exit(2)\n", "failed"),
            (self.emit({"type": "turn.completed"}) + "sys.exit(2)\n", "failed"),
            (self.emit({"type": "thread.started", "thread_id": "s"}), "failed"),
        ]:
            result = await self.run_cli(source)
            self.assertEqual(result.state, expected)
            self.assertNotIn("SECRET", result.message)

    async def test_terminal_failure_and_schema_errors_fail_fast(self):
        for value, state in [
            ({"type": "turn.failed", "error": {"message": "SECRET"}}, "failed"),
            ([], "protocol_error"),
            ({}, "protocol_error"),
            ({"type": "thread.started", "thread_id": 2}, "protocol_error"),
            ({"type": "item.completed", "item": []}, "protocol_error"),
            (
                {"type": "item.completed", "item": {"type": "agent_message", "text": 1}},
                "protocol_error",
            ),
            (
                {"type": "turn.completed", "usage": {"input_tokens": -1, "output_tokens": 1}},
                "protocol_error",
            ),
        ]:
            with self.subTest(value=value):
                result = await asyncio.wait_for(
                    self.run_cli(self.emit(value) + "time.sleep(30)\n"), 2
                )
                self.assertEqual(result.state, state)
                self.assertNotIn("SECRET", result.message)

    async def test_cleanup_permission_error_does_not_mask_protocol_failure(self):
        source = (
            self.emit({"type": "turn.completed", "usage": {"input_tokens": -1, "output_tokens": 1}})
            + "time.sleep(30)\n"
        )
        killpg = runner.os.killpg

        def signal_group(pid, sig):
            if sig == 0:
                raise PermissionError
            return killpg(pid, sig)

        with patch.object(runner.os, "killpg", side_effect=signal_group):
            result = await asyncio.wait_for(self.run_cli(source), 2)
        self.assertEqual(result.state, "protocol_error")

    async def test_bounds_and_stderr_drain(self):
        result = await asyncio.wait_for(
            self.run_cli("os.write(1, b'x' * 5000); time.sleep(30)\n", output_limit=1024), 2
        )
        self.assertEqual(result.state, "output_limit")
        source = "os.write(2, b'SECRET' * 50000)\n" + self.emit(
            *[
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "aggregated_output": "SECRET" * 50},
                }
                for _ in range(50)
            ],
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
            {"type": "turn.completed"},
        )
        self.assertEqual((await self.run_cli(source, output_limit=1024)).message, "ok")
        source = self.emit(
            *[
                {"type": "item.completed", "item": {"type": "agent_message", "text": "a" * 600}}
                for _ in range(3)
            ]
        )
        self.assertEqual((await self.run_cli(source, output_limit=1024)).state, "output_limit")

    async def test_cancel_timeout_and_external_cancel_kill_children(self):
        for mode in ("event", "timeout", "external"):
            with self.subTest(mode=mode):
                marker = self.root / "ready"
                marker.unlink(missing_ok=True)
                source = (
                    "import subprocess, signal\nfrom pathlib import Path\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    'subprocess.Popen([sys.executable, "-c", '
                    "\"import time; from pathlib import Path; time.sleep(0.6); Path('escaped').touch()\"] )\n"
                    "Path('ready').touch()\ntime.sleep(30)\n"
                )
                cancel = asyncio.Event()
                task = asyncio.create_task(
                    runner.run_codex(
                        "hi",
                        self.config(source, timeout=0.2 if mode == "timeout" else 0),
                        cancel=cancel,
                        terminate_grace=0.05,
                    )
                )
                async with asyncio.timeout(2):
                    while not marker.exists():
                        await asyncio.sleep(0.01)
                    if mode == "external":
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        if mode == "event":
                            cancel.set()
                        result = await task
                        self.assertEqual(
                            result.state, "cancelled" if mode == "event" else "timed_out"
                        )
                await asyncio.sleep(0.65)
                self.assertFalse((self.root / "escaped").exists())

    async def test_spawn_failure_preserves_metadata(self):
        result = await runner.run_codex("hi", self.config("", codex_bin="/missing/SECRET"))
        self.assertEqual(result.state, "failed")
        self.assertEqual((result.model, result.reasoning), ("test-model", "high"))
        self.assertNotIn("SECRET", result.message)

    async def test_new_session_command_and_invalid_bytes(self):
        source = (
            "assert sys.argv[1:] == ['exec', '--json', '--skip-git-repo-check', '-']\n"
        ) + self.emit({"type": "turn.completed"})
        self.assertEqual((await self.run_cli(source, model="", reasoning="")).state, "succeeded")
        for data in (b"not json\n", b"\xff\n", b'{"type":true}\n'):
            result = await self.run_cli(f"os.write(1, {data!r}); time.sleep(30)\n")
            self.assertEqual(result.state, "protocol_error")

    async def test_deadline_is_overall_despite_continuous_activity(self):
        source = (
            "while True:\n "
            + self.emit({"type": "item.started", "item": {"type": "command_execution"}}).rstrip()
            + "\n time.sleep(0.01)\n"
        )
        result = await asyncio.wait_for(self.run_cli(source, timeout=0.15), 2)
        self.assertEqual(result.state, "timed_out")
        self.assertEqual(
            result.message, "Overall execution deadline exceeded. Server acceptance may be unknown."
        )

    async def test_failure_exit_with_descendant_holding_pipes_fails_fast(self):
        source = (
            "import subprocess\n"
            'subprocess.Popen([sys.executable, "-c", '
            '"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"])\n'
            "time.sleep(0.1)\nsys.exit(2)\n"
        )
        result = await asyncio.wait_for(self.run_cli(source), 2)
        self.assertEqual(result.state, "failed")

    async def test_inherits_lease_fd_and_settings_grace(self):
        with (self.root / "lease").open("w") as lease:
            source = f"os.fstat({lease.fileno()})\n" + self.emit({"type": "turn.completed"})
            config = self.config(source, lease_fds=(lease.fileno(),), terminate_grace=0.05)
            self.assertEqual((await runner.run_codex("hi", config)).state, "succeeded")

    async def test_errors_use_safe_categories(self):
        for private, category in [
            ("authentication failed SECRET", "authentication"),
            ("connection reset by peer SECRET", "transport"),
        ]:
            for terminal in (False, True):
                source = (
                    self.emit({"type": "turn.failed", "error": {"message": private}})
                    if terminal
                    else f"sys.stderr.write({private!r}); sys.exit(1)\n"
                )
                result = await self.run_cli(source)
                self.assertEqual(result.state, "failed")
                self.assertIn(category, result.message.lower())
                self.assertNotIn("SECRET", result.message)

    async def test_resume_session_mismatch_is_not_published(self):
        events = []
        source = self.emit({"type": "thread.started", "thread_id": "other-private-id"})
        result = await runner.run_codex(
            "hi",
            self.config(source),
            session_id="requested-id",
            on_event=events.append,
            terminate_grace=0.05,
        )
        self.assertEqual(result.state, "protocol_error")
        self.assertEqual(result.session_id, "requested-id")
        self.assertNotIn("other-private-id", repr(events) + result.message)

    async def test_zero_exit_with_open_descendant_pipes_is_bounded(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                source = (
                    "import subprocess\n"
                    'subprocess.Popen([sys.executable, "-c", '
                    '"import time; from pathlib import Path; time.sleep(1); '
                    "Path('escaped').touch()\"])\n"
                ) + self.emit({"type": "thread.started", "thread_id": "s"})
                if terminal:
                    source += self.emit({"type": "turn.completed"})
                result = await asyncio.wait_for(self.run_cli(source), 2)
                self.assertEqual(result.state, "succeeded" if terminal else "failed")
                await asyncio.sleep(1.05)
                self.assertFalse((self.root / "escaped").exists())

    async def test_answer_phases_exclude_commentary_and_prefer_final(self):
        for messages, expected in [
            ([{"text": "one"}, {"text": "two"}], "one\ntwo"),
            ([{"text": "SECRET", "phase": "commentary"}], ""),
            (
                [
                    {"text": "fallback"},
                    {"text": "SECRET", "phase": "commentary"},
                    {"text": "answer", "phase": "final_answer"},
                    {"text": "trailing"},
                ],
                "answer",
            ),
        ]:
            source = self.emit(
                *[
                    {"type": "item.completed", "item": {"type": "agent_message", **message}}
                    for message in messages
                ],
                {"type": "turn.completed"},
            )
            result = await self.run_cli(source)
            self.assertEqual(result.state, "succeeded")
            self.assertEqual(result.message, expected)
