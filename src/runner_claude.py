"""Translate `claude -p --output-format stream-json` into the shared Event contract.

Verified against claude 2.1.266. That build emits `system` subtypes the published
type definitions do not list (`hook_started`, `hook_response`), which is exactly
why unknown records are ignored rather than treated as protocol errors.
"""

from time import monotonic

from events import Event, RunResult
from process import stream_jsonl

NAME = "claude"


def build_argv(binary, prompt, session_id=None, model=""):
    # The prompt goes in argv here: unlike codex, `claude -p` takes it as an
    # argument. This is exec, not a shell, so no quoting question arises.
    argv = [str(binary), "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if model:
        argv.extend(["--model", model])
    if session_id:
        argv.extend(["--resume", session_id])
    return argv


def new_state(session_id=None):
    return {"session": session_id, "answers": [], "final": "", "tokens": None,
            "turns": None, "error": ""}


def make_handler(state, on_event):
    """Build the record handler. Shared with the tests so they drive real code."""

    def on_record(record):
        kind = record.get("type")
        value = record.get("session_id")
        if isinstance(value, str) and value and value != state["session"]:
            state["session"] = value
            on_event(Event("session", value))
        if kind == "assistant":
            _assistant(record, state, on_event)
        elif kind == "user":
            _tool_results(record, on_event)
        elif kind == "result":
            _result(record, state)

    return on_record


async def run(settings, folder, prompt, session_id=None, on_event=lambda event: None,
              cancel=None):
    started = monotonic()
    state = new_state(session_id)
    on_record = make_handler(state, on_event)
    argv = build_argv(settings.claude_bin, prompt, session_id, settings.model)
    result, detail = await stream_jsonl(
        argv,
        folder,
        on_record,
        timeout=settings.timeout,
        limit=settings.output_limit,
        cancel=cancel,
    )
    message = state["final"] or "\n".join(state["answers"]).strip()
    if result != "succeeded":
        message = state["error"] or detail or message
    return RunResult(
        result,
        message or "The agent finished without a reply.",
        monotonic() - started,
        state["session"],
        state["tokens"],
        state["turns"],
    )


def _assistant(record, state, on_event):
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        state["answers"].append(content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            state["answers"].append(block["text"])
        elif block.get("type") == "tool_use":
            key = str(block.get("id") or block.get("name") or "tool")
            on_event(Event("tool", _describe(block), key))


def _tool_results(record, on_event):
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        key = str(block.get("tool_use_id") or "tool")
        on_event(Event("result", _text(block.get("content")), key,
                       failed=block.get("is_error") is True))


def _result(record, state):
    if isinstance(record.get("result"), str):
        state["final"] = record["result"]
    if isinstance(record.get("num_turns"), int):
        state["turns"] = record["num_turns"]
    usage = record.get("usage")
    if isinstance(usage, dict):
        counts = [usage.get(k) for k in ("input_tokens", "output_tokens")]
        if all(isinstance(c, int) and c >= 0 for c in counts):
            state["tokens"] = sum(counts)
    if record.get("is_error") is True:
        state["error"] = _tidy(str(record.get("result") or record.get("subtype") or "failed"))


def _describe(block):
    name = block.get("name") or "tool"
    args = block.get("input")
    if isinstance(args, dict):
        for field in ("command", "file_path", "pattern", "path", "query", "url"):
            value = args.get(field)
            if isinstance(value, str) and value.strip():
                return f"{name}: {_tidy(value)}"
    return str(name)


def _text(content):
    """Tool results arrive as a string or a list of content blocks."""
    if isinstance(content, str):
        return _tidy(content)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return _tidy(block["text"])
    return "done"


def _tidy(text, limit=140):
    line = next((s.strip() for s in text.splitlines() if s.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"
