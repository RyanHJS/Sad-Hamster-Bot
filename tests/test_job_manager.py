import asyncio
import tempfile
import unittest
from pathlib import Path

from bot_settings import Settings
from codex_runner import RunResult
from job_manager import JobManager
from job_store import JobStore


class ManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = Settings('secret', 1, 2, self.root, self.root / 'logs', 'codex')
        self.store = JobStore(self.root / 'state')
        self.addCleanup(self.store.close)

    def manager(self, runner):
        return JobManager(self.settings, self.store, runner=runner)

    async def test_followup_resume_and_explicit_new(self):
        sessions = []

        async def runner(prompt, settings, session_id=None, on_event=None, cancel=None):
            sessions.append(session_id)
            on_event({'kind': 'session', 'session_id': session_id or f'codex-{len(sessions)}'})
            return RunResult('succeeded', prompt, 1, settings.model, settings.reasoning)

        manager = self.manager(runner)
        first = manager.admit(10)
        await manager.execute(first, 'first')
        second = manager.admit(11)
        await manager.execute(second, 'follow-up')
        third = manager.admit(12, fresh=True)
        await manager.execute(third, 'unrelated')
        self.assertEqual(sessions, [None, 'codex-1', None])
        self.assertEqual(first['session_id'], second['session_id'])
        self.assertNotEqual(first['session_id'], third['session_id'])

    async def test_busy_duplicate_and_cancellation(self):
        entered = asyncio.Event()

        async def runner(prompt, settings, session_id=None, on_event=None, cancel=None):
            on_event({'kind': 'started', 'pid': 123})
            entered.set()
            await cancel.wait()
            return RunResult('cancelled', 'Cancelled.', 1, settings.model, settings.reasoning)

        manager = self.manager(runner)
        job = manager.admit(1)
        task = asyncio.create_task(manager.execute(job, 'long'))
        await entered.wait()
        with self.assertRaises(ValueError):
            manager.admit(2, fresh=True)
        self.assertIn('running', manager.status(job['id']))
        manager.cancel(job['id'])
        await task
        self.assertEqual(manager.job(job['id'])['state'], 'cancelled')
        with self.assertRaises(ValueError):
            manager.admit(1)

    async def test_timeout_is_not_replayed_and_requested_model_survives(self):
        calls = []

        async def runner(prompt, settings, **kwargs):
            calls.append(settings.model)
            return RunResult('timed_out', 'Overall deadline exceeded.', 2,
                             settings.model, settings.reasoning)

        manager = self.manager(runner)
        job = manager.admit(1)
        await manager.execute(job, 'work')
        self.assertEqual(len(calls), 1)
        summary = manager.job(job['id'])['result']
        self.assertIn(calls[0], summary)
        self.assertIn('timed_out', summary)
        self.assertEqual(len(self.store.attempts(job['id'])), 1)

    async def test_warning_does_not_count_as_agent_activity(self):
        entered = asyncio.Event()

        async def runner(prompt, settings, session_id=None, on_event=None, cancel=None):
            on_event({'kind': 'started', 'pid': 123})
            on_event({'kind': 'warning', 'text': 'Connection interrupted; retrying.'})
            entered.set()
            await cancel.wait()
            return RunResult('cancelled', 'Cancelled.', 0, settings.model, settings.reasoning)

        manager = self.manager(runner)
        job = manager.admit(1)
        task = asyncio.create_task(manager.execute(job, 'work'))
        await entered.wait()
        self.assertIn('not yet observed', manager.status(job['id']))
        manager.cancel(job['id'])
        await task

    async def test_unexpected_error_retains_attempt_and_releases_workspace(self):
        async def runner(*args, **kwargs):
            raise RuntimeError('private prompt')

        manager = self.manager(runner)
        job = manager.admit(1)
        await manager.execute(job, 'private prompt')
        result = manager.job(job['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('private prompt', result['result'])
        self.assertTrue(self.store.attempts(job['id']))
        manager.admit(2)

    async def test_session_ownership_and_selection(self):
        manager = self.manager(None)
        other = self.store.new_session('someone-else')
        with self.assertRaises(ValueError):
            manager.select_session(other['id'])
        first = manager.new_session()
        second = manager.new_session()
        self.assertNotEqual(first['id'], second['id'])
        manager.select_session(first['id'])
        self.assertEqual(manager.admit(1)['session_id'], first['id'])
