"""Both runners are driven by JSONL captured from the real CLIs.

Fixtures in tests/fixtures were recorded from codex-cli 0.153.4 and claude 2.1.266,
so these tests fail if a runner stops matching what the CLIs actually emit. They
call the same make_handler() that run() uses, so they cannot drift from production.
"""

import json
import unittest
from pathlib import Path

import runner_claude
import runner_codex

FIXTURES = Path(__file__).parent / "fixtures"


def replay(module, name):
    events = []
    state = module.new_state()
    handler = module.make_handler(state, events.append)
    for line in (FIXTURES / name).read_text().splitlines():
        if line.strip():
            handler(json.loads(line))
    return events, state


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.events, self.state = replay(runner_codex, "codex.jsonl")

    def test_reports_session_and_tokens(self):
        self.assertTrue(self.state["session"])
        self.assertEqual(self.events[0].kind, "session")
        self.assertGreater(self.state["tokens"], 0)

    def test_command_becomes_a_tool_then_a_result_under_one_key(self):
        tools = [e for e in self.events if e.kind == "tool"]
        results = [e for e in self.events if e.kind == "result"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(tools[0].key, results[0].key)
        self.assertIn("cat notes.txt", tools[0].value)
        self.assertFalse(results[0].failed)

    def test_final_answer_is_collected(self):
        self.assertIn("DONE", "\n".join(self.state["answers"]))

    def test_nonzero_exit_marks_the_result_failed(self):
        events = []
        state = runner_codex.new_state()
        handler = runner_codex.make_handler(state, events.append)
        item = {"id": "c1", "type": "command_execution", "command": "false",
                "exit_code": 1, "status": "failed", "aggregated_output": "boom"}
        handler({"type": "item.started", "item": item})
        handler({"type": "item.completed", "item": item})
        self.assertTrue(events[-1].failed)

    def test_argv_omits_model_flags_when_unset(self):
        argv = runner_codex.build_argv("codex", "hi")
        self.assertEqual(argv, ["codex", "exec", "--json", "--skip-git-repo-check", "-"])
        resumed = runner_codex.build_argv("codex", "hi", "sess-1", "gpt-5", "high")
        self.assertEqual(resumed[1:3], ["exec", "resume"])
        self.assertIn("sess-1", resumed)
        self.assertIn('model_reasoning_effort="high"', resumed)


class ClaudeTest(unittest.TestCase):
    def setUp(self):
        self.events, self.state = replay(runner_claude, "claude.jsonl")

    def test_tool_use_pairs_with_its_result(self):
        tools = [e for e in self.events if e.kind == "tool"]
        results = [e for e in self.events if e.kind == "result"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].key, results[0].key)
        self.assertTrue(tools[0].value.startswith("Read:"))
        self.assertFalse(results[0].failed)

    def test_final_result_and_turns_are_captured(self):
        self.assertIn("hello", self.state["final"])
        self.assertEqual(self.state["turns"], 2)

    def test_thinking_and_system_noise_produce_no_events(self):
        # The fixture holds 19 system/thinking_tokens records and a thinking block.
        kinds = {e.kind for e in self.events}
        self.assertLessEqual(kinds, {"session", "tool", "result"})

    def test_argv_shape(self):
        argv = runner_claude.build_argv("claude", "hi")
        self.assertEqual(argv[:6], ["claude", "-p", "hi", "--output-format",
                                    "stream-json", "--verbose"])
        self.assertIn("--resume", runner_claude.build_argv("claude", "hi", "s1"))


class ToleranceTest(unittest.TestCase):
    """An unknown or malformed record must never end a run."""

    JUNK = [
        {"type": "totally.new.event", "payload": {"a": 1}},
        {"type": "item.started"},
        {"type": "item.completed", "item": "not-a-dict"},
        {"type": "thread.started", "thread_id": None},
        {"type": "turn.completed", "usage": "nope"},
        {"no_type": True},
        {"type": "assistant", "message": {"content": None}},
        {"type": "user", "message": None},
        {"type": "result"},
        {"type": "system", "subtype": "hook_started"},
    ]

    def test_unknown_and_malformed_records_are_ignored(self):
        for module in (runner_codex, runner_claude):
            with self.subTest(runner=module.NAME):
                events = []
                state = module.new_state()
                handler = module.make_handler(state, events.append)
                for record in self.JUNK:
                    handler(record)  # must not raise
                self.assertEqual(events, [])
