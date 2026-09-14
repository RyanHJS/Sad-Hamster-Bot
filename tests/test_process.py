"""Real subprocesses: the point is that child tools never outlive a run."""

import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

import process


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def script(self, body):
        path = self.root / "fake-agent"
        path.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n{body}")
        path.chmod(0o700)
        return [str(path)]

    async def test_records_reach_the_handler_and_state_is_succeeded(self):
        argv = self.script(
            "print(json.dumps({'type':'thread.started','thread_id':'s1'}), flush=True)\n"
            "print('not json at all', flush=True)\n"
            "print(json.dumps({'type':'turn.completed'}), flush=True)\n"
        )
        seen = []
        state, detail = await process.stream_jsonl(argv, self.root, seen.append, grace=0.05)
        self.assertEqual(state, "succeeded", detail)
        # The non-JSON line is skipped rather than failing the run.
        self.assertEqual([r["type"] for r in seen], ["thread.started", "turn.completed"])

    async def test_nonzero_exit_reports_the_last_stderr_line(self):
        argv = self.script(
            "print('something broke badly', file=sys.stderr, flush=True)\nsys.exit(3)\n"
        )
        state, detail = await process.stream_jsonl(argv, self.root, lambda r: None, grace=0.05)
        self.assertEqual(state, "failed")
        self.assertIn("something broke badly", detail)

    async def test_prompt_is_written_to_stdin_not_argv(self):
        argv = self.script(
            "data = sys.stdin.read()\n"
            "print(json.dumps({'type':'echo','got':data}), flush=True)\n"
        )
        seen = []
        state, _ = await process.stream_jsonl(
            argv, self.root, seen.append, prompt="secret prompt", grace=0.05
        )
        self.assertEqual(state, "succeeded")
        self.assertEqual(seen[0]["got"], "secret prompt")

    async def test_missing_executable_is_a_clean_failure(self):
        state, detail = await process.stream_jsonl(
            [str(self.root / "nope")], self.root, lambda r: None, grace=0.05
        )
        self.assertEqual(state, "failed")
        self.assertIn("could not start", detail)

    async def test_deadline_stops_the_run(self):
        argv = self.script("time.sleep(30)\n")
        state, _ = await process.stream_jsonl(
            argv, self.root, lambda r: None, timeout=0.3, grace=0.05
        )
        self.assertEqual(state, "timed_out")

    async def test_discord_variables_are_withheld_from_the_agent(self):
        argv = self.script(
            "leaked = [k for k in os.environ if k.startswith('DISCORD_')]\n"
            "print(json.dumps({'type':'env','leaked':leaked}), flush=True)\n"
        )
        seen = []
        os.environ["DISCORD_BOT_TOKEN"] = "super-secret"
        self.addCleanup(os.environ.pop, "DISCORD_BOT_TOKEN", None)
        await process.stream_jsonl(argv, self.root, seen.append, grace=0.05)
        self.assertEqual(seen[0]["leaked"], [])

    async def test_cancelling_kills_the_child_tool_too(self):
        """A detached-looking grandchild must still die with the process group."""
        marker = self.root / "child-alive"
        argv = self.script(
            "import subprocess\n"
            f"marker = {str(marker)!r}\n"
            "subprocess.Popen([sys.executable, '-c',\n"
            "    'import time, sys\\n'\n"
            "    'open(sys.argv[1], \"w\").close()\\n'\n"
            "    'time.sleep(60)', marker])\n"
            "print(json.dumps({'type':'started'}), flush=True)\n"
            "time.sleep(60)\n"
        )
        cancel = asyncio.Event()
        seen = []

        async def stop_once_running():
            for _ in range(200):
                if seen:
                    break
                await asyncio.sleep(0.02)
            # Let the grandchild register itself before we tear the group down.
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(0.02)
            cancel.set()

        stopper = asyncio.create_task(stop_once_running())
        state, _ = await process.stream_jsonl(
            argv, self.root, seen.append, cancel=cancel, grace=0.05
        )
        await stopper
        self.assertEqual(state, "cancelled")
        self.assertTrue(marker.exists(), "grandchild never started; test proves nothing")
        await asyncio.sleep(0.2)
        self.assertEqual(self.leftover_pids(), [], "child tools survived cancellation")

    def leftover_pids(self):
        """PIDs of any surviving grandchild, found by the marker path in its argv."""
        import subprocess

        found = subprocess.run(
            ["pgrep", "-f", "child-alive"], capture_output=True, text=True
        ).stdout.split()
        return [int(p) for p in found if int(p) != os.getpid()]


class TerminateTest(unittest.IsolatedAsyncioTestCase):
    async def test_permission_denial_is_not_hidden_for_a_live_child(self):
        """Darwin can deny signals mid-teardown; a live child must still raise."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        fake = SimpleNamespace(
            pid=424242,
            returncode=None,
            stdout=asyncio.StreamReader(),
            stderr=asyncio.StreamReader(),
            wait=AsyncMock(),
        )
        with patch.object(process.os, "killpg", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                await process.terminate(fake, grace=0.05)
        fake.wait.assert_not_awaited()

    async def test_already_dead_group_is_not_an_error(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        fake = SimpleNamespace(
            pid=424243,
            returncode=0,
            stdout=asyncio.StreamReader(),
            stderr=asyncio.StreamReader(),
            wait=AsyncMock(),
        )
        fake.stdout.feed_eof()
        fake.stderr.feed_eof()
        with patch.object(process.os, "killpg", side_effect=ProcessLookupError):
            await process.terminate(fake, grace=0.05)
        fake.wait.assert_awaited()

    async def test_a_stubborn_child_is_escalated_from_term_to_kill(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        fake = SimpleNamespace(
            pid=424244,
            returncode=None,
            stdout=asyncio.StreamReader(),
            stderr=asyncio.StreamReader(),
            wait=AsyncMock(),
        )
        fake.stdout.feed_eof()
        fake.stderr.feed_eof()
        sent = []
        with patch.object(process.os, "killpg", side_effect=lambda pid, sig: sent.append(sig)):
            await process.terminate(fake, grace=0.05)
        self.assertEqual(sent[0], signal.SIGTERM)
        self.assertIn(signal.SIGKILL, sent)
