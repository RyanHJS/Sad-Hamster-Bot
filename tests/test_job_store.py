import sqlite3
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from job_store import JobStore


class JobStoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "state"
        self.store = JobStore(self.directory)
        self.addCleanup(lambda: self.store.close())

    def job(self, message_id=1, workspace="/work", scope="one"):
        session = self.store.new_session(scope)
        return self.store.create_job(scope, workspace, message_id, session["id"])

    def reopen(self):
        self.store.close()
        self.store = JobStore(self.directory)

    def test_scoped_sessions_and_selection_persist(self):
        self.assertIsNone(self.store.current_session("one"))
        first = self.store.create_session("one", "first")
        self.assertEqual(len(first["id"]), 32)
        self.assertIsNone(first["codex_id"])
        self.assertIsNone(self.store.current_session("one"))
        second = self.store.new_session("one")
        self.assertEqual(self.store.current_session("one"), second)
        self.store.set_codex_id(first["id"], "codex-thread")
        self.store.select_session("one", first["id"])
        with self.assertRaises(ValueError):
            self.store.select_session("two", first["id"])
        with self.assertRaises(ValueError):
            self.store.select_session("one", "missing")
        self.reopen()
        self.assertEqual(self.store.current_session("one")["codex_id"], "codex-thread")
        self.assertEqual({s["id"] for s in self.store.sessions("one")}, {first["id"], second["id"]})
        self.assertEqual(self.store.sessions("two"), [])

    def test_job_defaults_scoped_lookup_and_restart_without_reconciliation(self):
        job = self.job()
        for key, value in {
            "state": "accepted",
            "result": None,
            "status_message_id": None,
            "delivery": "pending",
            "scope": "one",
            "workspace": "/work",
            "source_message_id": 1,
        }.items():
            self.assertEqual(job[key], value)
        self.assertEqual(job["created"], job["updated"])
        self.assertIsNone(self.store.get_job("two", job["id"]))
        self.assertIsNone(self.store.get_job("two"))
        self.assertIsNone(self.store.get_job("one", "missing"))
        self.reopen()
        self.assertEqual(self.store.active_job("/work"), job)
        self.assertEqual(self.store.get_job("one"), job)

    def test_database_enforces_message_and_workspace_uniqueness_across_connections(self):
        job = self.job()
        other = JobStore(self.directory)
        self.addCleanup(other.close)
        session = other.new_session("two")
        with self.assertRaises(sqlite3.IntegrityError):
            other.create_job("two", "/else", 1, session["id"])
        for state in ("accepted", "starting", "running", "cancelling"):
            self.store.update_job(job["id"], state=state)
            with self.subTest(state=state), self.assertRaises(sqlite3.IntegrityError):
                other.create_job("two", "/work", 2, session["id"])
        self.store.update_job(job["id"], state="completed")
        new = other.create_job("two", "/work", 2, session["id"])
        self.assertEqual(other.active_job("/work")["id"], new["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update_job(job["id"], state="running")

    def test_job_session_must_belong_to_scope(self):
        session = self.store.new_session("one")
        with self.assertRaises(ValueError):
            self.store.create_job("two", "/work", 1, session["id"])
        with self.assertRaises(ValueError):
            self.store.create_job("one", "/work", 1, "missing")

    def test_concurrent_workspace_claim_has_exactly_one_winner(self):
        session = self.store.new_session("one")
        barrier = Barrier(2)

        def claim(message_id):
            store = JobStore(self.directory)
            try:
                barrier.wait(timeout=5)
                try:
                    return store.create_job("one", "/work", message_id, session["id"])
                except sqlite3.IntegrityError:
                    return None
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, (1, 2)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.active_job("/work"), winners[0])

    def test_concurrent_attempt_numbers_are_unique(self):
        job = self.job()
        barrier = Barrier(2)

        def attempt(model):
            store = JobStore(self.directory)
            try:
                barrier.wait(timeout=5)
                return store.add_attempt(job["id"], model, "high")
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ("model-a", "model-b")))
        self.assertEqual({result["number"] for result in results}, {1, 2})

    def test_latest_job_uses_creation_order_even_when_timestamps_tie(self):
        with patch("job_store.time.time", return_value=100):
            first = self.job()
            second = self.job(2, "/other")
        self.store.update_job(first["id"], state="completed")
        self.assertEqual(self.store.get_job("one")["id"], second["id"])

    def test_prune_uses_completion_age_and_preserves_active_payloads(self):
        with patch("job_store.time.time", return_value=100):
            old = self.job()
            active = self.job(2, "/active")
            self.store.update_job(active["id"], result="active-result")
            self.store.record_chunk(active["id"], 0, 90)
        self.store.update_job(old["id"], state="completed", result="just-finished")
        self.assertEqual(self.store.prune(), 0)
        self.assertEqual(self.store.get_job("one", old["id"])["result"], "just-finished")
        self.assertEqual(self.store.get_job("one", active["id"])["result"], "active-result")
        self.assertEqual(self.store.delivered_chunks(active["id"]), {0: 90})

    def test_updates_are_strict_and_persist(self):
        job = self.job()
        fields = dict(
            state="completed",
            result="answer",
            status_message_id=12,
            delivery="retry",
            elapsed=2.5,
            activity="done",
            pid=123,
        )
        self.store.update_job(job["id"], **fields)
        for field in ("id", "scope", "workspace", "created", "source_message_id", "bad"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.update_job(job["id"], **{field: "bad", "result": "corrupt"})
        with self.assertRaises(ValueError):
            self.store.update_job("missing", result="bad")
        self.reopen()
        saved = self.store.get_job("one", job["id"])
        for field, value in fields.items():
            self.assertEqual(saved[field], value)
        self.assertGreaterEqual(saved["updated"], saved["created"])

    def test_attempts_numbering_outcome_and_validation(self):
        job = self.job()
        self.assertEqual(self.store.attempts(job["id"]), [])
        first = self.store.add_attempt(job["id"], "model-a", "high")
        self.assertEqual(
            (first["number"], first["state"], first["elapsed"], first["tokens"]),
            (1, "running", 0, None),
        )
        second = self.store.add_attempt(job["id"], "model-b", "low")
        self.assertEqual(second["number"], 2)
        fields = dict(
            outcome="completed", elapsed=1.5, detail="ok", tokens=123, reported_model="actual-model"
        )
        self.store.update_attempt(first["id"], **fields)
        with self.assertRaises(ValueError):
            self.store.update_attempt(first["id"], model="bad")
        with self.assertRaises(ValueError):
            self.store.update_attempt("missing", outcome="failed")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_attempt("missing", "model", "high")
        self.reopen()
        attempts = self.store.attempts(job["id"])
        self.assertEqual([a["number"] for a in attempts], [1, 2])
        self.assertEqual(attempts[0]["state"], "completed")
        for field, value in fields.items():
            self.assertEqual(attempts[0][field], value)

    def test_pending_deliveries_and_chunks_survive_restart(self):
        active = self.job()
        complete = self.job(2, "/other")
        self.store.update_job(complete["id"], state="completed", result="answer")
        self.store.record_chunk(complete["id"], 0, 100)
        self.store.record_chunk(complete["id"], 0, 100)
        self.store.record_chunk(complete["id"], 1, 101)
        self.reopen()
        self.assertEqual(self.store.delivered_chunks(complete["id"]), {0: 100, 1: 101})
        self.assertEqual(self.store.delivered_chunks(active["id"]), {})
        self.assertEqual([j["id"] for j in self.store.pending_deliveries("one")], [complete["id"]])
        self.assertEqual(self.store.pending_deliveries("two"), [])
        self.store.update_job(complete["id"], delivery="retry")
        self.assertEqual(len(self.store.pending_deliveries("one")), 1)
        self.store.update_job(complete["id"], delivery="delivered")
        self.assertEqual(self.store.pending_deliveries("one"), [])

    def test_explicit_reconciliation_is_idempotent(self):
        jobs = []
        for index, state in enumerate(("accepted", "starting", "running", "cancelling")):
            job = self.job(index + 1, f"/work{index}")
            self.store.update_job(job["id"], state=state, pid=123)
            self.store.add_attempt(job["id"], "model", "high")
            jobs.append(job)
        terminal = self.job(10, "/finished")
        self.store.update_job(terminal["id"], state="completed", result="saved")
        self.assertEqual(self.store.reconcile_interrupted(), 4)
        self.assertEqual(self.store.reconcile_interrupted(), 0)
        for job in jobs:
            saved = self.store.get_job("one", job["id"])
            self.assertEqual(saved["state"], "interrupted")
            self.assertEqual(saved["delivery"], "pending")
            self.assertIn("interrupted", saved["result"].lower())
            self.assertIsNone(saved["pid"])
            self.assertIsNone(self.store.active_job(job["workspace"]))
            self.assertEqual(self.store.attempts(job["id"])[0]["state"], "interrupted")
        self.assertEqual(self.store.get_job("one", terminal["id"])["result"], "saved")

    def test_prune_expires_old_payloads_but_keeps_deduplication_and_sessions(self):
        with patch("job_store.time.time", return_value=100):
            old = self.job()
            self.store.add_attempt(old["id"], "model", "high")
            self.store.update_job(old["id"], state="completed", result="secret")
            self.store.record_chunk(old["id"], 0, 100)
            active = self.job(2, "/active")
        recent = self.job(3, "/recent")
        self.store.update_job(recent["id"], state="completed", result="recent")
        self.assertEqual(self.store.prune(), 1)
        self.assertIsNone(self.store.get_job("one", old["id"])["result"])
        self.assertEqual(self.store.delivered_chunks(old["id"]), {})
        self.assertEqual(self.store.attempts(old["id"]), [])
        self.assertEqual(self.store.active_job("/active"), active)
        self.assertEqual(self.store.current_session("one")["id"], recent["session_id"])
        self.assertEqual([j["id"] for j in self.store.pending_deliveries("one")], [recent["id"]])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_job("one", "/work", 1, old["session_id"])
        self.assertEqual(self.store.prune(), 0)
        with self.assertRaises(ValueError):
            self.store.prune(days=-1)

    def test_private_permissions_and_schema_version(self):
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        database = self.directory / "jobs.sqlite3"
        self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        database.chmod(0o644)
        self.directory.chmod(0o755)
        self.reopen()
        self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_message_lookup_is_scoped_and_preserves_selection(self):
        job = self.job()
        selected = self.store.new_session("one", "new selection")
        self.reopen()
        self.assertEqual(self.store.get_job_by_message("one", 1), job)
        self.assertIsNone(self.store.get_job_by_message("two", 1))
        self.assertIsNone(self.store.get_job_by_message("one", 999))
        self.assertEqual(self.store.current_session("one"), selected)

    def test_terminal_sending_becomes_uncertain_only_on_explicit_reconciliation(self):
        job = self.job()
        self.store.update_job(job["id"], state="completed", result="answer", delivery="sending")
        self.store.record_chunk(job["id"], 0, 100)
        self.reopen()
        before = self.store.get_job("one", job["id"])
        self.assertEqual(before["delivery"], "sending")
        self.assertEqual(self.store.pending_deliveries("one"), [])
        self.store.reconcile_interrupted()
        recovered = self.store.get_job("one", job["id"])
        self.assertEqual(recovered, {**before, "delivery": "uncertain"})
        self.assertEqual(self.store.pending_deliveries("one"), [recovered])
        self.assertEqual(self.store.pending_deliveries("two"), [])
        self.assertEqual(self.store.delivered_chunks(job["id"]), {0: 100})
        self.assertEqual(self.store.reconcile_interrupted(), 0)
        self.assertEqual(self.store.get_job("one", job["id"]), recovered)

    def test_uncertain_active_delivery_is_not_pending(self):
        job = self.job()
        self.store.update_job(job["id"], delivery="uncertain")
        self.assertEqual(self.store.pending_deliveries("one"), [])

    def test_symlink_database_is_rejected_without_touching_target(self):
        self.store.close()
        database = self.directory / "jobs.sqlite3"
        target = self.directory / "target.sqlite3"
        database.rename(target)
        target.chmod(0o644)
        contents = target.read_bytes()
        database.symlink_to(target)
        with self.assertRaises(OSError):
            JobStore(self.directory)
        self.assertEqual(target.read_bytes(), contents)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_unknown_schema_is_rejected(self):
        self.store.close()
        with sqlite3.connect(self.directory / "jobs.sqlite3") as connection:
            connection.execute("PRAGMA user_version = 999")
        with self.assertRaises(ValueError):
            JobStore(self.directory)


if __name__ == "__main__":
    unittest.main()
