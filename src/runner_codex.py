"""Translate `codex exec --json` events into the shared Event contract.

Unknown event types and unexpected shapes are ignored rather than fatal: the CLI
adds event types between versions, and a stray record should not fail a run.
"""

import json
from time import monotonic

from events import Event, RunResult
from process import stream_jsonl

NAME = "codex"

# Item types worth showing as a tool line, mapped to a human label.
TOOLS = {
    "command_execution": "Running a command",
    "file_change": "Editing files",
    "mcp_tool_call": "Calling a tool",
    "web_search": "Searching the web",
    "todo_list": "Updating the plan",
}


def build_argv(binary, prompt, session_id=None, model="", reasoning=""):
    argv = [str(binary), "exec"]
    if session_id:
        argv.append("resume")
    argv.extend(["--json", "--skip-git-repo-check"])
    if model:
        argv.extend(["--model", model])
    if reasoning:
        argv.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning)}"])
    if session_id:
        argv.append(session_id)
    argv.append("-")
    return argv


def new_state(session_id=None):
    return {"session": session_id, "tokens": None, "answers": []}


def make_handler(state, on_event):
    """Build the record handler. Shared with the tests so they drive real code."""

    def on_record(record):
        kind = record.get("type")
        if kind == "thread.started":
            value = record.get("thread_id")
            if isinstance(value, str) and value:
                state["session"] = value
                on_event(Event("session", value))
        elif kind in ("item.started", "item.completed"):
            _item(record, kind, state, on_event)
        elif kind == "turn.completed":
            usage = record.get("usage")
            if isinstance(usage, dict):
                counts = [usage.get(k) for k in ("input_tokens", "output_tokens")]
                if all(isinstance(c, int) and c >= 0 for c in counts):
                    state["tokens"] = sum(counts)
        elif kind == "turn.failed":
            error = record.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                on_event(Event("note", _tidy(error["message"])))
        elif kind == "error":
            if isinstance(record.get("message"), str):
                on_event(Event("note", _tidy(record["message"])))

    return on_record


async def run(settings, folder, prompt, session_id=None, on_event=lambda event: None,
              cancel=None):
    started = monotonic()
    state = new_state(session_id)
    on_record = make_handler(state, on_event)
    argv = build_argv(settings.codex_bin, prompt, session_id, settings.model, settings.reasoning)
    result, detail = await stream_jsonl(
        argv,
        folder,
        on_record,
        timeout=settings.timeout,
        limit=settings.output_limit,
        cancel=cancel,
        prompt=prompt,
    )
    message = "\n".join(state["answers"]).strip()
    if result != "succeeded":
        message = detail or message
    return RunResult(
        result,
        message or "The agent finished without a reply.",
        monotonic() - started,
        state["session"],
        state["tokens"],
    )


def _item(record, kind, state, on_event):
    item = record.get("item")
    if not isinstance(item, dict):
        return
    item_type, item_id = item.get("type"), item.get("id")
    if item_type == "agent_message":
        # Codex may revise its answer; a final_answer phase supersedes earlier text.
        if kind == "item.completed" and isinstance(item.get("text"), str):
            if item.get("phase") == "final_answer":
                state["answers"].clear()
            state["answers"].append(item["text"])
        return
    label = TOOLS.get(item_type)
    if not label:
        return
    key = str(item_id or item_type)
    if kind == "item.started":
        on_event(Event("tool", _describe(item, label), key))
    else:
        on_event(Event("result", _outcome(item), key, failed=_is_error(item)))


def _describe(item, label):
    for field in ("command", "path", "query"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return f"{label}: {_tidy(value)}"
    return label


def _outcome(item):
    for field in ("aggregated_output", "output", "result"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return _tidy(value)
    return "done"


def _is_error(item):
    code = item.get("exit_code")
    return item.get("status") == "failed" or (isinstance(code, int) and code != 0)


def _tidy(text, limit=140):
    """First non-empty line, trimmed — tool output can be enormous."""
    line = next((s.strip() for s in text.splitlines() if s.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"
